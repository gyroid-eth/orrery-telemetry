"""Provider account quota telemetry for the dashboard."""

from __future__ import annotations

from .antigravity import AntigravityQuotaProvider
from .claude import ClaudeQuotaProvider
from .claude_account import ClaudeAccountQuotaProvider
from .claude_route import ClaudeQuotaRoute
from .codex import CodexQuotaProvider
from .service import QuotaService


def build_default_service() -> QuotaService:
    return QuotaService(
        [
            ClaudeQuotaRoute(),
            CodexQuotaProvider(),
            AntigravityQuotaProvider(),
        ],
        default_ttl_seconds=60,
        stale_seconds=600,
    )


__all__ = [
    "AntigravityQuotaProvider",
    "ClaudeAccountQuotaProvider",
    "ClaudeQuotaProvider",
    "ClaudeQuotaRoute",
    "CodexQuotaProvider",
    "QuotaService",
    "build_default_service",
]
