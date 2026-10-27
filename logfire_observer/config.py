"""Environment-driven configuration for the Logfire observer plugin.

The plugin is stdlib-only by design, so configuration is a frozen dataclass
resolved from environment variables (no third-party settings library).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["LogfireConfig"]

DEFAULT_OTLP_ENDPOINT = "https://logfire-api.pydantic.dev/v1/traces"
DEFAULT_SERVICE_NAME = "hermes-agent"


def _env_bool(raw: str, default: bool) -> bool:
    """Parse a boolean env value, falling back to ``default`` when invalid."""
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _env_float(raw: str, default: float) -> float:
    """Parse a float env value, falling back to ``default`` when invalid."""
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(raw: str, default: int) -> int:
    """Parse an int env value, falling back to ``default`` when invalid."""
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class LogfireConfig:
    """Resolved plugin configuration.

    Attributes:
        endpoint: OTLP/HTTP JSON traces endpoint.
        token: Bearer token for the endpoint. Empty disables all exports.
        service_name: Value of the OTLP ``service.name`` resource attribute.
        enabled: Master switch; when False, hooks record nothing.
        include_content: When True, export truncated message/payload
            contents; when False (default) only sizes and counts are exported.
        flush_interval_seconds: Seconds between time-based batch flushes.
        max_batch_size: Buffered spans that trigger a size-based flush.
        timeout_seconds: HTTP timeout applied to each OTLP POST.
    """

    endpoint: str = DEFAULT_OTLP_ENDPOINT
    token: str = ""
    service_name: str = DEFAULT_SERVICE_NAME
    enabled: bool = True
    include_content: bool = False
    flush_interval_seconds: float = 5.0
    max_batch_size: int = 32
    timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LogfireConfig:
        """Build a configuration from environment variables.

        Args:
            env: Mapping to read; defaults to ``os.environ``. Injectable so
                tests stay hermetic.

        Returns:
            A resolved :class:`LogfireConfig`.
        """
        source = os.environ if env is None else env
        return cls(
            endpoint=source.get("LOGFIRE_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT),
            token=source.get("LOGFIRE_TOKEN", ""),
            service_name=source.get("LOGFIRE_SERVICE_NAME", DEFAULT_SERVICE_NAME),
            enabled=_env_bool(source.get("HERMES_LOGFIRE_ENABLED", "true"), True),
            include_content=_env_bool(
                source.get("HERMES_LOGFIRE_INCLUDE_CONTENT", "false"), False
            ),
            flush_interval_seconds=_env_float(
                source.get("HERMES_LOGFIRE_FLUSH_INTERVAL", "5"), 5.0
            ),
            max_batch_size=max(
                1, _env_int(source.get("HERMES_LOGFIRE_MAX_BATCH", "32"), 32)
            ),
            timeout_seconds=_env_float(source.get("HERMES_LOGFIRE_TIMEOUT", "5"), 5.0),
        )
