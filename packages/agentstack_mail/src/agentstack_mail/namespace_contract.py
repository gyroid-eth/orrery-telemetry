"""Candidate namespace contract; intentionally not wired to the live server.

Only explicit callers use this adapter. The published v1 tools, environment,
installer and database remain unchanged until the later cutover PR.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

NAMESPACE_CONTRACT_VERSION = 2
SCOPE_ARGUMENTS = frozenset({"project_key", "human_key", "to_project", "from_project"})


class NamespaceContractError(ValueError):
    """A bounded diagnostic which never includes argument values."""


def load_namespace_contract() -> dict[str, Any]:
    resource = files("agentstack_mail").joinpath("fixtures/namespace-tools-v2.json")
    try:
        raw = resource.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Match the existing authorization fixture's source/editable layout.
        source = (
            Path(__file__).resolve().parents[2] / "fixtures/namespace-tools-v2.json"
        )
        raw = source.read_text(encoding="utf-8")
    return json.loads(raw)


@dataclass(frozen=True, slots=True)
class NamespaceAdapter:
    """Strip only audited legacy scope arguments before candidate dispatch.

    This does not authenticate a caller or validate backend argument values.
    The dispatcher must retain its operation-specific validation/ownership
    boundary. Credentials and every non-scope argument pass through unchanged.
    """

    contract: Mapping[str, Any]

    def arguments(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        spec = self.contract["tools"].get(tool)
        if spec is None:
            raise NamespaceContractError("unknown namespace tool")
        ignored = frozenset(spec["accepted_ignored_arguments"])
        accepted = frozenset(spec["input_schema"]["properties"]) | ignored
        if set(arguments) - accepted:
            raise NamespaceContractError("unknown namespace arguments")
        normalized = deepcopy(
            {key: value for key, value in arguments.items() if key not in ignored}
        )
        if set(spec["input_schema"].get("required", [])) - set(normalized):
            raise NamespaceContractError("missing namespace arguments")
        return normalized

    def dispatch(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        backend: Callable[[str, dict[str, Any]], Any],
    ) -> Any:
        normalized = self.arguments(tool, arguments)
        if tool in self.contract["legacy_noop_tools"]:
            return {"namespace": "local", "created": False}
        return backend(tool, normalized)
