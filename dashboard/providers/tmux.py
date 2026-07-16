"""Thin adapter around the dashboard's existing tmux implementation.

The legacy functions remain in ``dashboard.server``. Keeping all calls behind
injection points lets the provider contract be tested without tmux and avoids
changing existing dashboard behaviour during the first extraction step.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .base import ActionResult, RuntimeProvider, RuntimeSnapshot

StateLoader = Callable[[], Mapping[str, Mapping[str, Any]]]
ActionHandler = Callable[[str], Mapping[str, Any]]


class TmuxRuntimeProvider(RuntimeProvider):
    """Expose legacy tmux state and actions through ``RuntimeProvider``."""

    provider_name = "tmux"
    capabilities = frozenset({"open", "wake", "kill"})

    def __init__(
        self,
        state_loader: StateLoader | None = None,
        action_handlers: Mapping[str, ActionHandler] | None = None,
    ) -> None:
        if state_loader is None or action_handlers is None:
            from dashboard import server

            state_loader = state_loader or server.tmux_state
            action_handlers = action_handlers or {
                "open": server.do_jump,
                "wake": server.do_resume,
                "kill": server.do_kill,
            }
        self._state_loader = state_loader
        self._action_handlers = dict(action_handlers)

    def list_runtimes(self) -> list[RuntimeSnapshot]:
        snapshots: list[RuntimeSnapshot] = []
        for external_id, raw in self._state_loader().items():
            title = str(raw.get("title") or "").strip()
            command = str(raw.get("cmd") or "")
            live = "" if title in ("", command, external_id) else title
            snapshots.append(
                RuntimeSnapshot(
                    external_id=external_id,
                    provider=self.provider_name,
                    present=True,
                    state="attached" if bool(raw.get("attached")) else "present",
                    live=live,
                    capabilities=self.capabilities,
                    metadata=dict(raw),
                )
            )
        return snapshots

    def perform(self, external_id: str, action: str) -> ActionResult:
        handler = self._action_handlers.get(action)
        if handler is None:
            return ActionResult(
                ok=False,
                external_id=external_id,
                action=action,
                error=f"unsupported tmux action: {action}",
            )
        try:
            raw = dict(handler(external_id))
        except Exception as exc:  # provider boundary: preserve dashboard process
            return ActionResult(
                ok=False,
                external_id=external_id,
                action=action,
                error=str(exc),
            )
        return ActionResult(
            ok=bool(raw.get("ok")),
            external_id=external_id,
            action=action,
            error=str(raw.get("error") or ""),
            details=raw,
        )
