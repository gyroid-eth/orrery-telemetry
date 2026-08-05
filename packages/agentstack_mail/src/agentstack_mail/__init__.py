"""AgentStack-owned coordination mail service.

The package is contract-only until the live compatibility fixtures and
source-attribution gates pass. It must not be used as a production server yet.
"""

from .contract import ISOLATION_DEFAULTS, SERVICE_IDENTITY

__all__ = ["ISOLATION_DEFAULTS", "SERVICE_IDENTITY"]
__version__ = "0.0.0"
