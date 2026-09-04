"""AgentStack-owned coordination mail service.

The first bootable core and loopback HTTP console entry point are available for
testing. Migration, supervision, and consumer cutover are not shipped yet.
"""

# Python 3.10 is part of the declared support floor. A few cutover modules use
# stdlib names introduced in Python 3.11; provide their exact 3.10 equivalents
# before any package submodule is imported. ``tomli`` is already a conditional
# dependency for Python < 3.11, and ``datetime.UTC`` is an alias of
# ``datetime.timezone.utc`` on newer interpreters.
import datetime as _datetime
import sys as _sys

if not hasattr(_datetime, "UTC"):  # pragma: no cover - Python 3.10 only
    _datetime.UTC = _datetime.timezone.utc  # type: ignore[attr-defined]

try:  # pragma: no cover - Python 3.11+ uses the stdlib module.
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as _tomllib
    _sys.modules.setdefault("tomllib", _tomllib)

from .contract import ISOLATION_DEFAULTS, SERVICE_IDENTITY

__all__ = ["ISOLATION_DEFAULTS", "SERVICE_IDENTITY"]
__version__ = "0.0.0"
