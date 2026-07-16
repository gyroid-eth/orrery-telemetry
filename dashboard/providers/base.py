"""Stable contracts for dashboard runtime backends.

Runtime providers describe execution surfaces only. Agent-mail remains the
source of truth for identities, messages, and graph edges.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Sanitized state for one runtime known to a provider."""

    external_id: str
    provider: str
    present: bool
    state: str
    live: str = ""
    capabilities: frozenset[str] = field(default_factory=frozenset)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Provider-neutral result returned by :meth:`RuntimeProvider.perform`."""

    ok: bool
    external_id: str
    action: str
    error: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)


class RuntimeProvider(ABC):
    """Interface implemented by every dashboard runtime backend."""

    @abstractmethod
    def list_runtimes(self) -> list[RuntimeSnapshot]:
        """Return the provider's current sanitized runtime inventory."""

    @abstractmethod
    def perform(self, external_id: str, action: str) -> ActionResult:
        """Perform a capability advertised by a runtime snapshot."""
