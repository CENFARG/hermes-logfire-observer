"""Hermetic tests for logfire_observer: no external network, no Hermes.

A fake plugin context records hook registrations, a recorder transport
captures OTLP POSTs in memory, and one test spins a loopback HTTP server to
prove the real urllib transport emits valid OTLP JSON.
"""

from __future__ import annotations

import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

from logfire_observer import plugin
from logfire_observer.config import LogfireConfig
from logfire_observer.otlp import (
    BatchExporter,
    build_span,
    derive_span_id,
    derive_trace_id,
    urllib_transport,
)
from logfire_observer.plugin import PluginState

API_PAYLOAD: dict[str, Any] = {
    "telemetry_schema_version": "hermes.observer.v1",
    "session_id": "sess-1", "task_id": "task-9", "turn_id": "turn-1",
    "api_request_id": "req-1", "api_call_count": 2,
    "model": "test-model", "provider": "test-provider", "base_url": "https://api.test/v1",
    "api_duration": 1.25, "finish_reason": "stop",
    "usage": {"input_tokens": 120, "output_tokens": 45},
    "request": {"messages": [{"role": "user", "content": "secret-plan"}]},
    "response": {"content": "reply"},
}


class FakeCtx:
    """Minimal Hermes plugin context that records registered hooks."""

    def __init__(self) -> None:
        self.hooks: dict[str, list[Any]] = {}

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks.setdefault(name, []).append(callback)


class Recorder:
    """Injectable transport capturing OTLP payloads instead of sending them."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, payload: dict[str, Any], token: str, timeout: float) -> int:
        self.calls.append({"url": url, "payload": payload, "token": token, "timeout": timeout})
        return self.status


def make_state(**overrides: Any) -> tuple[PluginState, Recorder]:
    """Build a :class:`PluginState` wired to an in-memory recorder."""
    settings = {"token": "test-token", "flush_interval_seconds": 3600.0, "max_batch_size": 1024}
    settings.update(overrides)
    config = LogfireConfig(**settings)
    recorder = Recorder()
    exporter = BatchExporter(
        endpoint="http://otlp.invalid/v1/traces", token=config.token,
        service_name="hermes-tests", flush_interval_seconds=config.flush_interval_seconds,
        max_batch_size=config.max_batch_size, transport=recorder,
    )
    return PluginState(config, exporter), recorder


def exported_spans(recorder: Recorder) -> list[dict[str, Any]]:
    """Flatten every span captured by the recorder across all calls."""
    spans: list[dict[str, Any]] = []
    for call in recorder.calls:
        for resource in call["payload"]["resourceSpans"]:
            for scope in resource["scopeSpans"]:
                spans.extend(scope["spans"])
    return spans


def attr_map(node: dict[str, Any]) -> dict[str, Any]:
    """Turn an OTLP attribute list back into a plain dict."""
    return {attr["key"]: next(iter(attr["value"].values())) for attr in node["attributes"]}


def _span(state: PluginState, recorder: Recorder) -> dict[str, Any]:
    """Flush exactly one buffered span and return it."""
    assert state.exporter.flush() is True
    spans = exported_spans(recorder)
    assert len(spans) == 1
    return spans[0]


def test_register_registers_all_observer_hooks() -> None:
    ctx = FakeCtx()
    state = plugin.register(ctx, config=LogfireConfig(token=""))
    assert set(ctx.hooks) == set(plugin.OBSERVER_HOOKS)
    for callbacks in ctx.hooks.values():
        for callback in callbacks:
            parameters = inspect.signature(callback).parameters
            assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
    assert isinstance(state, PluginState)


def test_post_api_request_span_attributes() -> None:
    state, recorder = make_state()
    state.handle("post_api_request", **API_PAYLOAD)
    assert state.exporter.accepted == 1
    attrs = attr_map(_span(state, recorder))
    assert attrs["gen_ai.system"] == "test-provider"
    assert attrs["gen_ai.request.model"] == "test-model"
    assert attrs["gen_ai.usage.input_tokens"] == 120
    assert attrs["gen_ai.usage.output_tokens"] == 45
    assert attrs["hermes.session_id"] == "sess-1"
    assert attrs["hermes.turn_id"] == "turn-1"
    assert attrs["hermes.api_call_count"] == 2
    assert attrs["hermes.finish_reason"] == "stop"
    assert attrs["hermes.status"] == "ok"
    assert attrs["hermes.telemetry_schema_version"] == "hermes.observer.v1"


def test_payloads_gated_by_default_and_exported_when_enabled() -> None:
    state, recorder = make_state()
    state.handle("post_api_request", **API_PAYLOAD)
    attrs = attr_map(_span(state, recorder))
    assert "hermes.request" not in attrs and "hermes.response" not in attrs
    assert attrs["hermes.request_chars"] > 0 and attrs["hermes.response_chars"] > 0
    assert "secret-plan" not in json.dumps(recorder.calls[0]["payload"])

    gated_state, gated_recorder = make_state(include_content=True)
    gated_state.handle("post_api_request", **API_PAYLOAD)
    gated_attrs = attr_map(_span(gated_state, gated_recorder))
    assert "secret-plan" in gated_attrs["hermes.request"]
    assert "reply" in gated_attrs["hermes.response"]


def test_trace_and_span_ids_are_stable_and_valid() -> None:
    assert derive_trace_id("sess-1", "turn-1") == derive_trace_id("sess-1", "turn-1")
    assert len(derive_trace_id("sess-1", "turn-1")) == 32
    assert derive_trace_id("sess-1", "turn-1") != derive_trace_id("sess-1", "turn-2")
    assert len(derive_span_id("post_tool_call:7")) == 16
    state, recorder = make_state()
    state.handle("post_api_request", **API_PAYLOAD)
    assert _span(state, recorder)["traceId"] == derive_trace_id("sess-1", "turn-1")


def test_api_duration_backdates_span_start() -> None:
    state, recorder = make_state()
    state.handle("post_api_request", **API_PAYLOAD)
    span = _span(state, recorder)
    elapsed_ns = int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])
    assert abs(elapsed_ns - 1_250_000_000) < 50_000_000


def test_post_tool_call_ok_and_error_attributes() -> None:
    state, recorder = make_state()
    base = {"session_id": "sess-1", "turn_id": "turn-1", "tool_name": "shell"}
    state.handle(
        "post_tool_call", **base, tool_call_id="call-1",
        args={"command": "ls"}, result="a.txt", duration_ms=12.5, status="ok",
    )
    state.handle(
        "post_tool_call", **base, tool_call_id="call-2",
        duration_ms=1.0, status="error", error_type="ToolError", error_message="boom",
    )
    assert state.exporter.flush() is True
    spans = exported_spans(recorder)
    ok_attrs = attr_map(spans[0])
    assert ok_attrs["hermes.tool_name"] == "shell"
    assert ok_attrs["hermes.status"] == "ok"
    assert ok_attrs["hermes.duration_ms"] == 12.5
    assert "hermes.tool_args" not in ok_attrs
    assert ok_attrs["hermes.tool_args_chars"] > 0
    error_attrs = attr_map(spans[1])
    assert error_attrs["hermes.status"] == "error"
    assert error_attrs["hermes.error_type"] == "ToolError"
    assert error_attrs["hermes.error_message"] == "boom"
    assert spans[1]["status"]["code"] == 2


def test_pre_llm_call_records_metadata_without_content() -> None:
    state, recorder = make_state()
    state.handle(
        "pre_llm_call",
        session_id="sess-1", turn_id="turn-1", user_message="hello there",
        conversation_history=[{"role": "user"}, {"role": "assistant"}],
        is_first_turn=True, model="test-model", platform="cli", sender_id="user-1",
    )
    attrs = attr_map(_span(state, recorder))
    assert attrs["hermes.is_first_turn"] is True
    assert attrs["hermes.history_entries"] == 2
    assert attrs["hermes.user_message_chars"] == len("hello there")
    assert "hermes.user_message" not in attrs
    assert attrs["hermes.platform"] == "cli"
    assert attrs["gen_ai.request.model"] == "test-model"


def test_post_llm_call_gates_assistant_response() -> None:
    state, recorder = make_state()
    state.handle(
        "post_llm_call", session_id="s", turn_id="t", assistant_response="final text", model="m"
    )
    attrs = attr_map(_span(state, recorder))
    assert attrs["hermes.assistant_response_chars"] == len("final text")
    assert "hermes.assistant_response" not in attrs


def test_api_request_error_attributes() -> None:
    state, recorder = make_state()
    state.handle(
        "api_request_error",
        session_id="sess-1", turn_id="turn-1", api_request_id="req-9",
        model="test-model", provider="test-provider", api_duration=0.4,
        status_code=429, retry_count=1, max_retries=3, retryable=True,
        reason="rate_limited", error={"type": "RateLimitError", "message": "slow down"},
    )
    span = _span(state, recorder)
    attrs = attr_map(span)
    assert attrs["hermes.status"] == "error"
    assert attrs["hermes.error_type"] == "RateLimitError"
    assert attrs["hermes.error_message"] == "slow down"
    assert attrs["hermes.status_code"] == 429
    assert attrs["hermes.retryable"] is True
    assert attrs["hermes.reason"] == "rate_limited"
    assert span["status"]["code"] == 2


def test_session_end_records_outcome() -> None:
    state, recorder = make_state()
    state.handle(
        "on_session_end", session_id="sess-1", turn_id="turn-1",
        completed=True, interrupted=False, reason="finished",
    )
    attrs = attr_map(_span(state, recorder))
    assert attrs["hermes.completed"] is True
    assert attrs["hermes.interrupted"] is False
    assert attrs["hermes.reason"] == "finished"


def test_datamarking_token_is_exported_as_attribute() -> None:
    state, recorder = make_state()
    state.handle(
        "post_llm_call", session_id="s", turn_id="t",
        assistant_response="ok", datamarking_token="DM-123",
    )
    assert attr_map(_span(state, recorder))["hermes.datamarking_token"] == "DM-123"


def test_empty_token_counts_spans_without_exporting() -> None:
    recorder = Recorder()
    exporter = BatchExporter(
        endpoint="https://logfire-api.pydantic.dev/v1/traces",
        token="", service_name="hermes-tests", transport=recorder,
    )
    for index in range(3):
        exporter.add(build_span(
            name="genai.test", trace_id=derive_trace_id("s", "t"),
            span_id=derive_span_id(f"seed-{index}"), attributes={}, start_ns=1, end_ns=1,
        ))
    assert exporter.flush() is False
    assert exporter.exported == 0
    assert exporter.dropped_no_token == 3
    assert recorder.calls == []


def test_batch_flushes_at_max_batch_size() -> None:
    state, recorder = make_state(max_batch_size=3)
    for index in range(3):
        state.handle("on_session_start", session_id=f"s{index}", turn_id="t")
    assert state.exporter.exported == 3
    assert state.exporter.in_memory == 0
    assert len(recorder.calls) == 1


def test_transport_failure_is_fail_open() -> None:
    class ExplodingRecorder:
        def __call__(self, url: str, payload: dict[str, Any], token: str, timeout: float) -> int:
            raise ConnectionError("network down")

    exporter = BatchExporter(
        endpoint="http://otlp.invalid/v1/traces", token="test-token",
        service_name="hermes-tests", transport=ExplodingRecorder(),
    )
    exporter.add(build_span(
        name="genai.test", trace_id="a" * 32, span_id="b" * 16,
        attributes={}, start_ns=1, end_ns=1,
    ))
    assert exporter.flush() is False
    assert exporter.dropped_error == 1


def test_disabled_config_records_nothing() -> None:
    state, recorder = make_state(enabled=False)
    state.handle("post_tool_call", session_id="s", turn_id="t", tool_name="shell")
    assert state.exporter.accepted == 0
    assert state.exporter.flush() is False
    assert recorder.calls == []


class _CaptureHandler(BaseHTTPRequestHandler):
    captured: ClassVar[dict[str, Any]] = {}

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        _CaptureHandler.captured = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(self.rfile.read(length).decode("utf-8")),
        }
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


def test_urllib_transport_posts_valid_otlp_to_local_server() -> None:
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        exporter = BatchExporter(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
            token="local-test-token", service_name="hermes-test-service",
            transport=urllib_transport,
        )
        exporter.add(build_span(
            name="genai.post_tool_call", trace_id=derive_trace_id("sess", "turn"),
            span_id=derive_span_id("seed"), attributes={"hermes.tool_name": "shell"},
            start_ns=1, end_ns=2,
        ))
        assert exporter.flush() is True
        assert exporter.exported == 1
    finally:
        server.shutdown()
        server.server_close()

    captured = _CaptureHandler.captured
    assert captured["authorization"] == "Bearer local-test-token"
    resource = captured["body"]["resourceSpans"][0]["resource"]
    assert attr_map(resource)["service.name"] == "hermes-test-service"
    span = captured["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["name"] == "genai.post_tool_call"
    assert attr_map(span)["hermes.tool_name"] == "shell"
