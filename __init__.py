"""Hermes plugin entry point.

Hermes imports the plugin directory as a package and calls the
``register(ctx)`` function found in this file (native plugins require both
``plugin.yaml`` and a loadable ``__init__.py``). All implementation lives in
the :mod:`logfire_observer` subpackage.
"""

try:
    # Hermes loads the plugin directory as a package: relative import.
    from .logfire_observer.plugin import register
except ImportError:
    # Repo root placed on sys.path directly (pytest, pip layout): absolute.
    from logfire_observer.plugin import register

__version__ = "0.1.0"

__all__ = ["__version__", "register"]
