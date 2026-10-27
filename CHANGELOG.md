# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-27

### Added

- Initial standalone `logfire_observer` plugin for Hermes Agent.
- Read-only Observer Hooks bridge: 9 hooks (`pre/post_api_request`,
  `api_request_error`, `pre/post_tool_call`, `pre/post_llm_call`,
  `on_session_start/end`) → OpenTelemetry GenAI spans
  (`telemetry_schema_version = "hermes.observer.v1"`).
- OTLP/HTTP JSON exporter on stdlib `urllib` with in-memory batching
  (flush at 32 spans or 5 s), fail-open error handling, and counters
  (`accepted` / `exported` / `dropped_no_token` / `dropped_error`).
- Stable span identity: `traceId = sha256(session:turn)[:32]`,
  `spanId = sha256(hook:sequence)[:16]`; payload durations backdate spans.
- Env-based configuration: `LOGFIRE_OTLP_ENDPOINT`, `LOGFIRE_TOKEN`,
  `LOGFIRE_SERVICE_NAME`, `HERMES_LOGFIRE_ENABLED`,
  `HERMES_LOGFIRE_INCLUDE_CONTENT`, `HERMES_LOGFIRE_FLUSH_INTERVAL`,
  `HERMES_LOGFIRE_MAX_BATCH`, `HERMES_LOGFIRE_TIMEOUT`.
- Empty-token safe mode: no export, in-memory counters only (runnable
  without a Logfire account).
- Privacy defaults: sanitized Hermes payloads only, content gating
  (sizes/counts instead of contents) and truncation.
- Hermetic pytest suite (16 tests): fake plugin context, recorder transport,
  loopback HTTP server asserting the OTLP wire format.

### Integrity (sha256)

Computed over the released files at this version:

| File | SHA256 |
| --- | --- |
| `plugin.yaml` | `91e2ab4cea240ae6fa69075a2ac99c6c1cdabc7e050e1b897baec54eddf38d90` |
| `__init__.py` | `e005f206e966b1fe2d8dfd4ba4934aad59bdaaf56b5aac7a95bacd6ecd09a398` |
| `logfire_observer/__init__.py` | `482ec1d2711974e1c24d6391e723ae6ba99a218e86b8cdd9bf8b1a88c7d5855e` |
| `logfire_observer/plugin.py` | `8823e754ef156021d5a0eb351dc94ed0056c0aa42a235a2f565ec6be13f98679` |
| `logfire_observer/otlp.py` | `d20594ae5de32d872f0aa891140bdc89580d6e9190fa27522c4b0f90808585de` |
| `logfire_observer/config.py` | `c35ea7b7765a40c43153e4c215a5519a7a367544b4d04bb17878daa6438947b2` |
| `tests/test_plugin.py` | `8e485d716437bccc5465f1edcbeae56deb1fc85add8a809eeecddc98863ab214` |
| `pyproject.toml` | `9788436a439e4db38120afb821aa08db0ccbcdae7c5304761cef86e495c78a36` |

### Notes

- No `LOGFIRE_TOKEN` is shipped or required; the token belongs to the
  deployment owner.
- `gen_ai.system` follows OTel GenAI semantics (provider; falls back to the
  model name when the payload omits `provider`).
