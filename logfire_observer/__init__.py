"""Read-only Hermes observer plugin exporting telemetry to Logfire via OTLP."""

from .config import LogfireConfig
from .plugin import OBSERVER_HOOKS, TELEMETRY_SCHEMA_VERSION, PluginState, register

__version__ = "0.1.0"

__all__ = [
    "OBSERVER_HOOKS",
    "TELEMETRY_SCHEMA_VERSION",
    "LogfireConfig",
    "PluginState",
    "__version__",
    "register",
]
