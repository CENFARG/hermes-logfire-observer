"""Hermes observer hook bridge: turns hook payloads into OTel GenAI spans.

Strictly read-only: every callback only records what happened. Behavior-
changing hooks (context injection, tool blocking, result/output transforms)
are deliberately NOT registered, and every callback always returns None.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from .config import LogfireConfig
from .otlp import BatchExporter, build_span, derive_span_id, derive_trace_id

logger = logging.getLogger("logfire_observer.plugin")

TELEMETRY_SCHEMA_VERSION = "hermes.observer.v1"

OBSERVER_HOOKS: tuple[str, ...] = (
    "pre_api_request",
    "post_api_request",
    "api_request_error",
    "pre_tool_call",
    "post_tool_call",
    "pre_llm_call",
    "post_llm_call",
    "on_session_start",
    "on_session_end",
)

_CORRELATION_KEYS: tuple[str, ...] = (
    "session_id",
    "task_id",
    "turn_id",
    "api_request_id",
    "api_call_count",
    "tool_call_id",
    "parent_session_id",
    "child_session_id",
    "parent_subagent_id",
    "child_subagent_id",
    "model",
    "platform",
    "sender_id",
)

_MAX_TEXT = 512
_MAX_PAYLOAD = 2000


def _text(value: Any) -> str | None:
    """Return ``value`` as a trimmed non-empty string, else None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_number(value: Any) -> bool:
    """Return True for real numbers (bools excluded)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _truncate(value: Any, limit: int) -> str:
    """Serialize ``value`` to a bounded string safe for export."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(value)
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


def _without_nones(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop attributes whose value is None so absent fields stay absent."""
    return {key: value for key, value in attrs.items() if value is not None}


def _generic_attributes(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Build correlation attributes shared by every hook payload."""
    attrs: dict[str, Any] = {"hermes.telemetry_schema_version": TELEMETRY_SCHEMA_VERSION}
    for key in _CORRELATION_KEYS:
        value = kwargs.get(key)
        if value is None:
            continue
        if key == "api_call_count":
            if _is_number(value):
                attrs["hermes.api_call_count"] = int(value)
            continue
        attrs[f"hermes.{key}"] = _text(value)
    return attrs


def _datamarking_attributes(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Expose datamarking defense tokens as correlation attributes."""
    return {
        f"hermes.{key}": _truncate(value, _MAX_TEXT)
        for key, value in kwargs.items()
        if "datamarking" in key.lower() and value is not None
    }


def _usage_attributes(usage: Any) -> dict[str, Any]:
    """Map a provider usage mapping onto GenAI semantic-convention keys."""
    if not isinstance(usage, dict):
        return {}
    attrs: dict[str, Any] = {}
    for key in ("input_tokens", "prompt_tokens", "inputTokens"):
        if _is_number(usage.get(key)):
            attrs["gen_ai.usage.input_tokens"] = int(usage[key])
            break
    for key in ("output_tokens", "completion_tokens", "outputTokens"):
        if _is_number(usage.get(key)):
            attrs["gen_ai.usage.output_tokens"] = int(usage[key])
            break
    return attrs


def _payload_attribute(attrs: dict[str, Any], name: str, value: Any, include: bool) -> None:
    """Attach a payload attribute, honoring the content-gating privacy flag.

    When content export is disabled (default), only the serialized size is
    recorded under ``<name>_chars``; the content itself never leaves the host.
    """
    if value is None:
        return
    if include:
        attrs[f"hermes.{name}"] = _truncate(value, _MAX_PAYLOAD)
    else:
        attrs[f"hermes.{name}_chars"] = len(_truncate(value, 10**9))


def _genai_identity(kwargs: dict[str, Any]) -> dict[str, Any]:
    """GenAI semconv identity: system is the provider (OTel convention)."""
    return {
        "gen_ai.system": _text(kwargs.get("provider")) or _text(kwargs.get("model")) or "unknown",
        "gen_ai.request.model": _text(kwargs.get("model")),
    }


def _pre_api_attrs(kwargs: dict[str, Any], config: LogfireConfig) -> dict[str, Any]:
    """Attributes for pre_api_request (provider attempt started)."""
    attrs = {**_genai_identity(kwargs), "hermes.status": "started"}
    _payload_attribute(attrs, "request", kwargs.get("request"), config.include_content)
    return _without_nones(attrs)


def _api_request_attrs(kwargs: dict[str, Any], config: LogfireConfig) -> dict[str, Any]:
    """Attributes for post_api_request (successful provider attempt)."""
    attrs: dict[str, Any] = {
        **_genai_identity(kwargs),
        "hermes.status": "ok",
        "hermes.base_url": _text(kwargs.get("base_url")),
        "hermes.finish_reason": _text(kwargs.get("finish_reason")),
        "hermes.response_model": _text(kwargs.get("response_model")),
    }
    if _is_number(kwargs.get("api_duration")):
        attrs["hermes.api_duration"] = float(kwargs["api_duration"])
    attrs.update(_usage_attributes(kwargs.get("usage")))
    _payload_attribute(attrs, "request", kwargs.get("request"), config.include_content)
    _payload_attribute(attrs, "response", kwargs.get("response"), config.include_content)
    return _without_nones(attrs)


def _error_attributes(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Extract error identity from structured or scalar error fields."""
    error = kwargs.get("error")
    error_type = _text(kwargs.get("error_type"))
    message: Any = kwargs.get("error_message")
    if isinstance(error, dict):
        error_type = _text(error.get("type")) or error_type
        message = error.get("message", message)
    elif error is not None:
        message = error
    if not error_type:
        error_type = type(error).__name__ if error is not None else "unknown"
    attrs: dict[str, Any] = {"hermes.status": "error", "hermes.error_type": error_type}
    if message is not None:
        attrs["hermes.error_message"] = _truncate(message, _MAX_TEXT)
    if _text(kwargs.get("reason")):
        attrs["hermes.reason"] = _text(kwargs["reason"])
    if _is_number(kwargs.get("status_code")):
        attrs["hermes.status_code"] = int(kwargs["status_code"])
    if isinstance(kwargs.get("retryable"), bool):
        attrs["hermes.retryable"] = kwargs["retryable"]
    return attrs


def _api_error_attrs(kwargs: dict[str, Any], config: LogfireConfig) -> dict[str, Any]:
    """Attributes for api_request_error (failed provider attempt)."""
    attrs = _api_request_attrs(kwargs, config)
    attrs.update(_error_attributes(kwargs))
    return attrs


def _tool_attrs(kwargs: dict[str, Any], config: LogfireConfig, *, finished: bool) -> dict[str, Any]:
    """Attributes for pre/post_tool_call lifecycle spans."""
    attrs: dict[str, Any] = {"hermes.tool_name": _text(kwargs.get("tool_name")) or "unknown"}
    if not finished:
        attrs["hermes.status"] = "started"
        return attrs
    attrs["hermes.status"] = _text(kwargs.get("status")) or "ok"
    if _is_number(kwargs.get("duration_ms")):
        attrs["hermes.duration_ms"] = float(kwargs["duration_ms"])
    if any(kwargs.get(key) is not None for key in ("error", "error_type", "error_message")):
        attrs.update(
            {
                key: value
                for key, value in _error_attributes(kwargs).items()
                if key != "hermes.status"
            }
        )
    _payload_attribute(attrs, "tool_args", kwargs.get("args"), config.include_content)
    _payload_attribute(attrs, "tool_result", kwargs.get("result"), config.include_content)
    return attrs


def _pre_llm_attrs(kwargs: dict[str, Any], config: LogfireConfig) -> dict[str, Any]:
    """Attributes for pre_llm_call (turn-level, content-gated)."""
    attrs: dict[str, Any] = {**_genai_identity(kwargs)}
    if isinstance(kwargs.get("is_first_turn"), bool):
        attrs["hermes.is_first_turn"] = kwargs["is_first_turn"]
    history = kwargs.get("conversation_history")
    if history is not None:
        try:
            attrs["hermes.history_entries"] = len(history)
        except TypeError:
            pass
    _payload_attribute(attrs, "user_message", kwargs.get("user_message"), config.include_content)
    return attrs


def _post_llm_attrs(kwargs: dict[str, Any], config: LogfireConfig) -> dict[str, Any]:
    """Attributes for post_llm_call (turn summary, content-gated)."""
    attrs: dict[str, Any] = {**_genai_identity(kwargs)}
    _payload_attribute(
        attrs, "assistant_response", kwargs.get("assistant_response"), config.include_content
    )
    return attrs


def _session_end_attrs(kwargs: dict[str, Any], config: LogfireConfig) -> dict[str, Any]:
    """Attributes for on_session_end (run outcome)."""
    attrs: dict[str, Any] = {}
    for key in ("completed", "interrupted"):
        if isinstance(kwargs.get(key), bool):
            attrs[f"hermes.{key}"] = kwargs[key]
    if _text(kwargs.get("reason")):
        attrs["hermes.reason"] = _text(kwargs["reason"])
    return attrs


_BUILDERS: dict[str, Callable[[dict[str, Any], LogfireConfig], dict[str, Any]]] = {
    "pre_api_request": _pre_api_attrs,
    "post_api_request": _api_request_attrs,
    "api_request_error": _api_error_attrs,
    "pre_tool_call": lambda kwargs, config: _tool_attrs(kwargs, config, finished=False),
    "post_tool_call": lambda kwargs, config: _tool_attrs(kwargs, config, finished=True),
    "pre_llm_call": _pre_llm_attrs,
    "post_llm_call": _post_llm_attrs,
    "on_session_start": lambda kwargs, config: {},
    "on_session_end": _session_end_attrs,
}


def _start_ns(kwargs: dict[str, Any], end_ns: int) -> int:
    """Backdate the span start using payload durations when available."""
    for key, scale in (("duration_ms", 1_000_000), ("api_duration", 1_000_000_000)):
        if _is_number(kwargs.get(key)) and kwargs[key] >= 0:
            return end_ns - int(kwargs[key] * scale)
    return end_ns


class PluginState:
    """Per-registration state: config, batch exporter, and hook dispatch."""

    def __init__(self, config: LogfireConfig, exporter: BatchExporter) -> None:
        """Store config and exporter; handlers are built per hook."""
        self.config = config
        self.exporter = exporter
        self._sequence = 0

    def handle(self, hook: str, **kwargs: Any) -> None:
        """Handle one hook invocation, buffering a span (fail-open)."""
        if not self.config.enabled:
            return
        try:
            self._build_and_add(hook, kwargs)
        except Exception:
            # Fail-open boundary: any error is logged, never raised into the agent loop.
            logger.exception("logfire_observer: failed to record hook %s", hook)

    def handler_for(self, hook: str) -> Callable[[Any], None]:
        """Return a ``**kwargs`` callback bound to this state for ``hook``."""

        def _handler(**kwargs: Any) -> None:
            self.handle(hook, **kwargs)

        return _handler

    def _build_and_add(self, hook: str, kwargs: dict[str, Any]) -> None:
        """Assemble attributes, build the span, and hand it to the exporter."""
        self._sequence += 1
        end_ns = time.time_ns()
        session_id = _text(kwargs.get("session_id")) or "unknown"
        turn_id = _text(kwargs.get("turn_id")) or "unknown"
        attributes = _generic_attributes(kwargs)
        attributes.update(_datamarking_attributes(kwargs))
        attributes.update(_BUILDERS[hook](kwargs, self.config))
        span = build_span(
            name=f"genai.{hook}",
            trace_id=derive_trace_id(session_id, turn_id),
            span_id=derive_span_id(f"{hook}:{self._sequence}"),
            attributes=attributes,
            start_ns=_start_ns(kwargs, end_ns),
            end_ns=end_ns,
            status_code=2 if attributes.get("hermes.status") == "error" else 1,
        )
        self.exporter.add(span)


def register(ctx: Any, config: LogfireConfig | None = None) -> PluginState:
    """Register the read-only observer hooks on a Hermes plugin context.

    Args:
        ctx: Plugin context exposing ``register_hook(name, callback)``.
        config: Optional pre-resolved configuration; defaults to the
            environment-derived :meth:`LogfireConfig.from_env`.

    Returns:
        The wired :class:`PluginState` (useful for tests and diagnostics).
    """
    cfg = config or LogfireConfig.from_env()
    exporter = BatchExporter(
        endpoint=cfg.endpoint,
        token=cfg.token,
        service_name=cfg.service_name,
        flush_interval_seconds=cfg.flush_interval_seconds,
        max_batch_size=cfg.max_batch_size,
        timeout_seconds=cfg.timeout_seconds,
    )
    state = PluginState(cfg, exporter)
    for hook in OBSERVER_HOOKS:
        ctx.register_hook(hook, state.handler_for(hook))
    logger.info(
        "logfire_observer: registered %d observer hooks (export=%s, endpoint=%s)",
        len(OBSERVER_HOOKS),
        "enabled" if cfg.enabled and cfg.token else "disabled",
        cfg.endpoint,
    )
    return state
