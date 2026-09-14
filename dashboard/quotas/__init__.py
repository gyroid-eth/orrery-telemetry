"""Provider account quota telemetry for the dashboard."""

from __future__ import annotations

from .antigravity import AntigravityQuotaProvider
from .claude import ClaudeQuotaProvider
from .codex import CodexQuotaProvider
from .service import QuotaService


def build_default_service() -> QuotaService:
    return QuotaService(
        [
            ClaudeQuotaProvider(),
            CodexQuotaProvider(),
            AntigravityQuotaProvider(),
        ],
        default_ttl_seconds=60,
        stale_seconds=600,
    )


__all__ = [
    "AntigravityQuotaProvider",
    "ClaudeQuotaProvider",
    "CodexQuotaProvider",
    "QuotaService",
    "build_default_service",
]
