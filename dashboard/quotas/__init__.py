"""Provider account quota telemetry for the dashboard."""

from __future__ import annotations

from .antigravity import AntigravityQuotaProvider
from .claude import ClaudeQuotaProvider
from .claude_account import ClaudeAccountQuotaProvider
from .claude_route import ClaudeQuotaRoute
from .codex import CodexQuotaProvider
from .service import QuotaService
from .deepseek import DeepSeekBalanceProvider


def build_default_service(*, dsh_enabled=None) -> QuotaService:
    return QuotaService(
        [
            ClaudeQuotaRoute(),
            CodexQuotaProvider(),
            AntigravityQuotaProvider(),
            *([DeepSeekBalanceProvider(dsh_enabled)] if dsh_enabled is not None else []),
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
