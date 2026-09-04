"""AgentStack-owned coordination mail service.

The first bootable core and loopback HTTP console entry point are available for
testing. Migration, supervision, and consumer cutover are not shipped yet.
"""

from .contract import ISOLATION_DEFAULTS, SERVICE_IDENTITY

__all__ = ["ISOLATION_DEFAULTS", "SERVICE_IDENTITY"]
__version__ = "0.0.0"
