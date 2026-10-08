"""Pure namespace planning, receipt and final-validation gates for PR1.

Inputs are explicit snapshots/evidence supplied by a later trusted collector.
This module does not read a Mail database, stop a writer, or activate cutover.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any
from uuid import uuid4

from .utils import sanitize_agent_name

PLANNING_PHASES = ("planned", "prepared", "prevalidated", "quiesced", "final_verified")
FINAL_CHECKS = frozenset(
    {
        "agent_names_unique",
        "window_mapping_complete",
        "thread_mapping_complete",
        "lease_anchors_resolved",
        "message_ids_preserved",
        "recipient_read_ack_preserved",
        "credential_generations_preserved",
        "attachments_preserved",
        "fts_valid",
        "delivery_states_preserved",
        "contact_policy_preserved",
        "bindings_verified",
        "block_hashes_verified",
    }
)


class PlanningError(ValueError):
    """Fixed diagnostics, without snapshot values or credentials."""


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentRecord:
    agent_id: int
    source: str
    name: str
    retired: bool = False


@dataclass(frozen=True, slots=True)
class AgentNamePlan:
    names: Mapping[int, str]
    conflicts: tuple[tuple[int, ...], ...]


def plan_agent_names(
    records: Sequence[AgentRecord], rename_map: Mapping[int, str] | None = None
) -> AgentNamePlan:
    """Use exact and sanitized lowercase lookup keys, including retired rows."""
    rename_map = rename_map or {}
    ids = {record.agent_id for record in records}
    if (
        len(ids) != len(records)
        or any(type(key) is not int or key <= 0 for key in ids)
        or set(rename_map) - ids
    ):
        raise PlanningError("INVALID_AGENT_IDS")
    names: dict[int, str] = {}
    keys: dict[int, set[str]] = {}
    for record in records:
        name = rename_map.get(record.agent_id, record.name)
        if (
            not isinstance(name, str)
            or len(name) > 128
            or not sanitize_agent_name(name)
        ):
            raise PlanningError("INVALID_AGENT_NAME")
        names[record.agent_id] = name
        keys[record.agent_id] = {name.lower(), sanitize_agent_name(name).lower()}
    # Union lookup aliases without a quadratic scan of unrelated agents.
    parents = {key: key for key in ids}

    def root(key: int) -> int:
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    owners: dict[str, int] = {}
    for key in sorted(ids):
        for lookup in keys[key]:
            if lookup in owners:
                parents[root(key)] = root(owners[lookup])
            else:
                owners[lookup] = key
    groups: dict[int, list[int]] = {}
    for key in sorted(ids):
        groups.setdefault(root(key), []).append(key)
    conflicts = tuple(
        sorted(tuple(group) for group in groups.values() if len(group) > 1)
    )
    return AgentNamePlan(names, conflicts)


@dataclass(frozen=True, slots=True)
class WindowRecord:
    row_id: int
    source: str
    window_uuid: str
    display_name: str
    bindings: tuple[str, ...] = ()
    expired: bool = False


@dataclass(frozen=True, slots=True)
class WindowPlan:
    targets: Mapping[int, str]
    unresolved: tuple[int, ...]


def plan_windows(
    records: Sequence[WindowRecord],
    choices: Mapping[int, str] | None = None,
    *,
    prior_targets: Mapping[int, str] | None = None,
    uuid_factory: Callable[[], str] | None = None,
) -> WindowPlan:
    """No automatic collision winner; generated choices replay from receipt."""
    choices = choices or {}
    prior_targets = prior_targets or {}
    factory = uuid_factory or (lambda: uuid4().hex)
    ids = {record.row_id for record in records}
    if (
        len(ids) != len(records)
        or any(type(key) is not int or key <= 0 for key in ids)
        or (set(choices) | set(prior_targets)) - ids
    ):
        raise PlanningError("INVALID_WINDOW_IDS")
    counts: dict[str, int] = {}
    for record in records:
        if (
            not isinstance(record.window_uuid, str)
            or not record.window_uuid
            or len(record.window_uuid) > 64
        ):
            raise PlanningError("INVALID_WINDOW_UUID")
        counts[record.window_uuid] = counts.get(record.window_uuid, 0) + 1
    targets: dict[int, str] = {}
    unresolved: set[int] = set()
    for record in sorted(records, key=lambda row: row.row_id):
        choice = choices.get(record.row_id)
        if choice is None and counts[record.window_uuid] > 1:
            unresolved.add(record.row_id)
            continue
        if choice is None or choice == "keep":
            value = record.window_uuid
        elif choice == "generate":
            if record.row_id in prior_targets:
                value = prior_targets[record.row_id]
            else:
                for _attempt in range(64):
                    value = factory()
                    if value not in counts and value not in targets.values():
                        break
                else:
                    raise PlanningError("WINDOW_UUID_COLLISION")
        elif isinstance(choice, str):
            value = choice
        else:
            raise PlanningError("INVALID_WINDOW_CHOICE")
        if not isinstance(value, str) or not value or len(value) > 64:
            raise PlanningError("INVALID_WINDOW_UUID")
        if record.row_id in prior_targets and value != prior_targets[record.row_id]:
            raise PlanningError("WINDOW_PLAN_CHANGED")
        targets[record.row_id] = value
    for value in set(targets.values()):
        duplicates = {key for key, target in targets.items() if target == value}
        if len(duplicates) > 1:
            unresolved.update(duplicates)
    return WindowPlan(
        {key: value for key, value in targets.items() if key not in unresolved},
        tuple(sorted(unresolved)),
    )


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    generation: str
    manifest_json: str

    @classmethod
    def capture(cls, generation: str, manifest: Mapping[str, Any]) -> SourceSnapshot:
        if not isinstance(generation, str) or not generation:
            raise PlanningError("INVALID_SOURCE_GENERATION")
        return cls(generation, _json(manifest))

    @property
    def digest(self) -> str:
        return fingerprint(
            {"generation": self.generation, "manifest": json.loads(self.manifest_json)}
        )


@dataclass(frozen=True, slots=True)
class FenceEvidence:
    generation: str
    expected_writers: tuple[str, ...]
    writer_states: Mapping[str, str]
    common_authority_held: bool = False
    supervisors_suppressed: bool = False
    connections_closed: bool = False
    inflight_wakes: int | None = None

    def ready(self, generation: str) -> bool:
        expected = set(self.expected_writers)
        return bool(
            self.generation == generation
            and expected
            and len(expected) == len(self.expected_writers)
            and set(self.writer_states) == expected
            and all(state == "stopped" for state in self.writer_states.values())
            and self.common_authority_held is True
            and self.supervisors_suppressed is True
            and self.connections_closed is True
            and type(self.inflight_wakes) is int
            and self.inflight_wakes == 0
        )


@dataclass(frozen=True, slots=True)
class CutoverGate:
    ready: bool
    reasons: tuple[str, ...]
    # PR1 never enables a state switch, even for a fully validated fixture.
    activation_enabled: bool = False


@dataclass(frozen=True, slots=True)
class FinalValidationEvidence:
    """Bind actual check results to the snapshot that the checker examined."""

    source_digest: str
    candidate_digest: str
    results: tuple[tuple[str, str], ...]

    @classmethod
    def record(
        cls,
        snapshot: SourceSnapshot,
        candidate_manifest: Mapping[str, Any],
        results: Mapping[str, str],
    ) -> FinalValidationEvidence:
        # The future data verifier calls this after running its checks. The
        # planner cannot infer FTS/attachment/owner correctness from counts.
        return cls(
            snapshot.digest,
            fingerprint(candidate_manifest),
            tuple(sorted(results.items())),
        )


@dataclass(frozen=True, slots=True)
class MigrationReceipt:
    receipt_id: str
    planned_source_digest: str
    resolutions_json: str
    phase: str = "planned"
    final_source_digest: str | None = None
    final_generation: str | None = None
    final_candidate_digest: str | None = None
    final_checks: tuple[str, ...] = ()
    version: int = 1

    @classmethod
    def create(
        cls, snapshot: SourceSnapshot, resolutions: Mapping[str, Any]
    ) -> MigrationReceipt:
        return cls(uuid4().hex, snapshot.digest, _json(resolutions))

    def advance(self, phase: str) -> MigrationReceipt:
        if (self.phase, phase) not in {
            ("planned", "prepared"),
            ("prepared", "prevalidated"),
        }:
            raise PlanningError("INVALID_PLANNING_TRANSITION")
        return replace(self, phase=phase)

    def quiesce(self, fence: FenceEvidence) -> MigrationReceipt:
        if self.phase != "prevalidated" or not fence.ready(fence.generation):
            raise PlanningError("WRITER_FENCE_UNCONFIRMED")
        return replace(self, phase="quiesced")

    def verify_final(
        self,
        snapshot: SourceSnapshot,
        candidate_manifest: Mapping[str, Any],
        evidence: FinalValidationEvidence,
        fence: FenceEvidence,
    ) -> MigrationReceipt:
        if self.phase != "quiesced" or not fence.ready(snapshot.generation):
            raise PlanningError("WRITER_FENCE_UNCONFIRMED")
        if (
            evidence.source_digest != snapshot.digest
            or evidence.candidate_digest != fingerprint(candidate_manifest)
        ):
            raise PlanningError("FINAL_VALIDATION_GENERATION_CHANGED")
        checks = dict(evidence.results)
        if (
            len(checks) != len(evidence.results)
            or set(checks) != FINAL_CHECKS
            or any(result != "pass" for result in checks.values())
        ):
            raise PlanningError("FINAL_VALIDATION_INCOMPLETE")
        resolutions = json.loads(self.resolutions_json)
        if resolutions.get("unresolved") != []:
            raise PlanningError("UNRESOLVED_PLAN")
        if candidate_manifest.get("resolutions_digest") != fingerprint(resolutions):
            raise PlanningError("FINAL_RESOLUTIONS_CHANGED")
        return replace(
            self,
            phase="final_verified",
            final_source_digest=snapshot.digest,
            final_generation=snapshot.generation,
            final_candidate_digest=fingerprint(candidate_manifest),
            final_checks=tuple(sorted(checks)),
        )

    def gate(
        self,
        snapshot: SourceSnapshot,
        candidate_manifest: Mapping[str, Any],
        fence: FenceEvidence,
    ) -> CutoverGate:
        reasons: list[str] = []
        if self.phase != "final_verified" or set(self.final_checks) != FINAL_CHECKS:
            reasons.append("FINAL_VALIDATION_REQUIRED")
        if (
            self.final_source_digest != snapshot.digest
            or self.final_generation != snapshot.generation
        ):
            reasons.append("SOURCE_GENERATION_CHANGED")
        if self.final_candidate_digest != fingerprint(candidate_manifest):
            reasons.append("CANDIDATE_CHANGED")
        resolutions = json.loads(self.resolutions_json)
        if resolutions.get("unresolved") != [] or candidate_manifest.get(
            "resolutions_digest"
        ) != fingerprint(resolutions):
            reasons.append("RESOLUTIONS_CHANGED")
        if not fence.ready(snapshot.generation):
            reasons.append("WRITER_FENCE_UNCONFIRMED")
        return CutoverGate(not reasons, tuple(reasons))

    def to_json(self) -> str:
        payload = asdict(self)
        return _json({"payload": payload, "integrity": fingerprint(payload)})

    @classmethod
    def from_json(cls, raw: str) -> MigrationReceipt:
        try:
            document = json.loads(raw)
            payload = document["payload"]
            if set(document) != {"payload", "integrity"} or document[
                "integrity"
            ] != fingerprint(payload):
                raise ValueError
            payload["final_checks"] = tuple(payload["final_checks"])
            receipt = cls(**payload)

            def digest_shape(value: Any, length: int = 64) -> bool:
                return (
                    isinstance(value, str)
                    and len(value) == length
                    and all(char in "0123456789abcdef" for char in value)
                )

            if (
                type(receipt.version) is not int
                or receipt.version != 1
                or receipt.phase not in PLANNING_PHASES
            ):
                raise ValueError
            if not digest_shape(receipt.receipt_id, 32) or not digest_shape(
                receipt.planned_source_digest
            ):
                raise ValueError
            if not isinstance(json.loads(receipt.resolutions_json), dict):
                raise ValueError
            if receipt.phase == "final_verified" and (
                set(receipt.final_checks) != FINAL_CHECKS
                or not digest_shape(receipt.final_source_digest)
                or not isinstance(receipt.final_generation, str)
                or not receipt.final_generation
                or not digest_shape(receipt.final_candidate_digest)
            ):
                raise ValueError
            if receipt.phase != "final_verified" and any(
                (
                    receipt.final_source_digest,
                    receipt.final_generation,
                    receipt.final_candidate_digest,
                    receipt.final_checks,
                )
            ):
                raise ValueError
            return receipt
        except (TypeError, ValueError, KeyError):
            raise PlanningError("INVALID_RECEIPT") from None
