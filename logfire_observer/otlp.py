"""OTLP/HTTP JSON export built on the Python standard library only.

Spans follow the OTLP JSON (protobuf-JSON) encoding: 64-bit integers are
serialized as strings and attribute values use ``stringValue`` / ``intValue``
/ ``boolValue`` / ``doubleValue`` wrappers. The exporter batches spans in
memory and flushes by size or elapsed time; every failure mode is fail-open.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("logfire_observer.otlp")

Transport = Callable[[str, dict[str, Any], str, float], int]
"""Sends one OTLP payload; returns the HTTP status code."""


def derive_trace_id(session_id: str, turn_id: str) -> str:
    """Derive a stable 16-byte OTel trace id from session and turn ids."""
    digest = hashlib.sha256(f"{session_id}:{turn_id}".encode()).hexdigest()
    return digest[:32]


def derive_span_id(seed: str) -> str:
    """Derive a stable 8-byte OTel span id from an arbitrary seed string."""
    material = seed if seed else str(time.time_ns())
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _otlp_value(value: Any) -> dict[str, Any]:
    """Wrap a Python scalar in the OTLP JSON AnyValue encoding."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def build_span(
    *,
    name: str,
    trace_id: str,
    span_id: str,
    attributes: dict[str, Any],
    start_ns: int,
    end_ns: int,
    status_code: int = 1,
) -> dict[str, Any]:
    """Build one OTLP JSON span record.

    Args:
        name: Span name, e.g. ``genai.post_api_request``.
        trace_id: 32-character hex trace id.
        span_id: 16-character hex span id.
        attributes: Span attributes (scalars only; others are stringified).
        start_ns: Start timestamp in Unix nanoseconds.
        end_ns: End timestamp in Unix nanoseconds.
        status_code: OTel status code (1 = OK, 2 = ERROR).

    Returns:
        A dict matching the OTLP JSON span shape.
    """
    otlp_attributes = [
        {"key": key, "value": _otlp_value(value)}
        for key, value in sorted(attributes.items())
    ]
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": otlp_attributes,
        "status": {"code": status_code},
    }


def otlp_payload(service_name: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap buffered spans into an OTLP ``ExportTraceServiceRequest``."""
    resource = {
        "attributes": [
            {"key": "service.name", "value": {"stringValue": service_name}}
        ]
    }
    return {
        "resourceSpans": [
            {
                "resource": resource,
                "scopeSpans": [
                    {
                        "scope": {"name": "logfire_observer", "version": "0.1.0"},
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def urllib_transport(
    url: str, payload: dict[str, Any], token: str, timeout: float
) -> int:
    """POST one OTLP JSON payload with urllib and return the HTTP status.

    Args:
        url: OTLP endpoint.
        payload: OTLP JSON document.
        token: Bearer token (header omitted when empty).
        timeout: Socket timeout in seconds.

    Raises:
        Exception: Any network or HTTP error; callers are fail-open.
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status)


class BatchExporter:
    """In-memory batching OTLP exporter with fail-open error handling.

    Lifecycle: ``ready`` -> ``flushing`` -> ``ready`` -> ... -> ``closed``.
    Missing tokens and network failures never raise to the caller: the
    affected spans are counted and logged, keeping the agent loop safe.
    """

    def __init__(
        self,
        endpoint: str,
        token: str,
        service_name: str,
        *,
        flush_interval_seconds: float = 5.0,
        max_batch_size: int = 32,
        timeout_seconds: float = 5.0,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the exporter.

        Args:
            endpoint: OTLP/HTTP JSON traces endpoint.
            token: Bearer token; an empty token disables all exports.
            service_name: ``service.name`` resource attribute value.
            flush_interval_seconds: Time-based flush threshold.
            max_batch_size: Size-based flush threshold.
            timeout_seconds: HTTP timeout per export attempt.
            transport: Injectable transport (defaults to urllib); tests pass
                recorders so no network is touched.
            clock: Injectable monotonic clock for hermetic timing tests.
        """
        self._endpoint = endpoint
        self._token = token
        self._service_name = service_name
        self._flush_interval = flush_interval_seconds
        self._max_batch = max(1, int(max_batch_size))
        self._timeout = timeout_seconds
        self._transport: Transport = transport or urllib_transport
        self._clock = clock
        self._lock = threading.Lock()
        self._buffer: list[dict[str, Any]] = []
        self._last_flush = self._clock()
        self.accepted = 0
        self.exported = 0
        self.dropped_no_token = 0
        self.dropped_error = 0
        self.state = "ready"

    @property
    def in_memory(self) -> int:
        """Number of spans currently buffered in memory."""
        with self._lock:
            return len(self._buffer)

    def add(self, span: dict[str, Any]) -> None:
        """Buffer one span, flushing when size or time thresholds are hit."""
        due = False
        with self._lock:
            if self.state == "closed":
                logger.debug("Exporter closed: dropping span %s.", span.get("name"))
                return
            self._buffer.append(span)
            self.accepted += 1
            due = len(self._buffer) >= self._max_batch or (
                self._clock() - self._last_flush >= self._flush_interval
            )
        if due:
            self.flush()

    def flush(self) -> bool:
        """Export all buffered spans once; returns True when fully exported."""
        with self._lock:
            if self.state == "closed" or not self._buffer:
                return False
            spans = self._buffer[:]
            self._buffer.clear()
            self.state = "flushing"
        try:
            ok = self._export(spans)
        finally:
            with self._lock:
                self._last_flush = self._clock()
                if self.state == "flushing":
                    self.state = "ready"
        return ok

    def close(self) -> None:
        """Flush remaining spans and stop accepting new ones."""
        self.flush()
        with self._lock:
            self.state = "closed"

    def _export(self, spans: list[dict[str, Any]]) -> bool:
        """Send one batch; classify failures into counters (never raises)."""
        count = len(spans)
        if not self._token:
            self.dropped_no_token += count
            logger.debug(
                "LOGFIRE_TOKEN empty: %d spans counted in memory, not exported.",
                count,
            )
            return False
        payload = otlp_payload(self._service_name, spans)
        try:
            status = self._transport(self._endpoint, payload, self._token, self._timeout)
        except Exception as exc:  # noqa: BLE001 -- fail-open boundary: any network error is logged, never raised
            self.dropped_error += count
            logger.warning("OTLP export failed for %d spans: %s", count, exc)
            return False
        if 200 <= status < 300:
            self.exported += count
            logger.debug(
                "Exported %d spans to %s (HTTP %d).", count, self._endpoint, status
            )
            return True
        self.dropped_error += count
        logger.warning("OTLP export got HTTP %d for %d spans.", status, count)
        return False
