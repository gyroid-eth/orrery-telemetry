"""AgentStack-owned coordination mail service.

The first bootable core is available for differential testing. Transport,
migration, supervision, and consumer cutover are intentionally not shipped yet.
"""

from .contract import ISOLATION_DEFAULTS, SERVICE_IDENTITY

__all__ = ["ISOLATION_DEFAULTS", "SERVICE_IDENTITY"]
__version__ = "0.0.0"
