"""Thread issuance and legacy resolution for explicit candidate plans only.

No database is opened here. PR3 will persist this catalog and its uniqueness
constraints; PR1 makes the issuing/resolution contract executable in fixtures.
Caller evidence comes from the trusted binding boundary, never tool arguments.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Iterable
from uuid import uuid4

from .utils import validate_thread_id_format


class ThreadResolutionError(ValueError):
    def __init__(self, code: str, candidates: int = 0):
        self.code = code
        self.candidates = candidates
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CallerEvidence:
    agent_id: int | None
    confirmed: bool
    legacy_sources: frozenset[str] = frozenset()

    def require_agent(self) -> int:
        if (
            self.confirmed is not True
            or type(self.agent_id) is not int
            or self.agent_id <= 0
        ):
            raise ThreadResolutionError("CALLER_UNCONFIRMED")
        return self.agent_id


@dataclass(frozen=True, slots=True)
class LegacyThread:
    record_id: int
    source: str
    alias: str
    participants: frozenset[int]
    message_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class ThreadDecision:
    record_ids: tuple[int, ...]
    action: str
    confirmed: bool = False
    relation_unknown: bool = False


@dataclass(frozen=True, slots=True)
class CanonicalThread:
    thread_id: str
    participants: frozenset[int]
    label: str | None = None


class ThreadCatalog:
    """A candidate-owned catalog; issuance is atomic within this plan.

    It cannot mutate the live Mail DB or supply a global default. UUID values
    and legacy decisions are exported with the receipt for stable replay.
    """

    def __init__(
        self,
        records: Iterable[LegacyThread] = (),
        *,
        uuid_factory: Callable[[], str] | None = None,
    ):
        records = tuple(records)
        self.records = {record.record_id: record for record in records}
        if len(self.records) != len(records):
            raise ThreadResolutionError("DUPLICATE_THREAD_RECORD")
        self._validate_records(records)
        self.threads: dict[str, CanonicalThread] = {}
        self.record_targets: dict[int, str] = {}
        self.message_targets: dict[int, str] = {}
        self.message_participants: dict[int, frozenset[int]] = {}
        self.relation_unknown: set[int] = set()
        self._uuid_factory = uuid_factory or (lambda: uuid4().hex)
        self._lock = RLock()

    @staticmethod
    def _validate_records(records: tuple[LegacyThread, ...]) -> None:
        messages: set[int] = set()
        for record in records:
            if (
                type(record.record_id) is not int
                or record.record_id <= 0
                or not isinstance(record.source, str)
                or not record.source
                or not isinstance(record.alias, str)
                or not validate_thread_id_format(record.alias)
            ):
                raise ThreadResolutionError("INVALID_THREAD_RECORD")
            if not record.participants or any(
                type(value) is not int or value <= 0
                for value in record.participants | record.message_ids
            ):
                raise ThreadResolutionError("INVALID_THREAD_RECORD")
            if messages & record.message_ids:
                raise ThreadResolutionError("DUPLICATE_MESSAGE_ID")
            messages.update(record.message_ids)

    def _issue(self, participants: frozenset[int], label: str | None = None) -> str:
        if label is not None and (not isinstance(label, str) or len(label) > 128):
            raise ThreadResolutionError("INVALID_THREAD_LABEL")
        aliases = {record.alias for record in self.records.values()}
        for _attempt in range(64):
            value = self._uuid_factory()
            if len(value) != 32 or any(
                char not in "0123456789abcdef" for char in value
            ):
                raise ThreadResolutionError("INVALID_THREAD_UUID")
            thread_id = "th-" + value
            if thread_id not in self.threads and thread_id not in aliases:
                self.threads[thread_id] = CanonicalThread(
                    thread_id, participants, label
                )
                return thread_id
        raise ThreadResolutionError("THREAD_ID_COLLISION")

    def create(self, caller: CallerEvidence, *, label: str | None = None) -> str:
        agent_id = caller.require_agent()
        with self._lock:
            return self._issue(frozenset({agent_id}), label)

    def apply(self, decisions: Iterable[ThreadDecision]) -> None:
        """Validate the entire decision set before modifying the catalog.

        Duplicate aliases require explicit group/separate decisions. A group
        requires confirmation of related deliveries; similarity is not proof.
        """
        decisions = tuple(decisions)
        with self._lock:
            seen: set[int] = set()
            groups: list[tuple[tuple[int, ...], bool]] = []
            for decision in decisions:
                ids = decision.record_ids
                if (
                    not ids
                    or len(ids) != len(set(ids))
                    or any(key not in self.records for key in ids)
                ):
                    raise ThreadResolutionError("INVALID_THREAD_DECISION")
                if seen & set(ids):
                    raise ThreadResolutionError("THREAD_DECISION_ALREADY_APPLIED")
                if (
                    decision.action not in {"group", "separate"}
                    or not decision.confirmed
                ):
                    raise ThreadResolutionError("THREAD_CONFIRMATION_REQUIRED")
                if decision.action == "group" and decision.relation_unknown:
                    raise ThreadResolutionError("THREAD_RELATION_UNKNOWN")
                seen.update(ids)
                if any(key in self.record_targets for key in ids):
                    if not all(key in self.record_targets for key in ids):
                        raise ThreadResolutionError("THREAD_DECISION_CHANGED")
                    targets = {self.record_targets[key] for key in ids}
                    expected_targets = 1 if decision.action == "group" else len(ids)
                    if len(targets) != expected_targets or any(
                        (key in self.relation_unknown) != decision.relation_unknown
                        for key in ids
                    ):
                        raise ThreadResolutionError("THREAD_DECISION_CHANGED")
                    continue
                if decision.action == "group":
                    groups.append((ids, False))
                else:
                    groups.extend(((key,), decision.relation_unknown) for key in ids)
            for record in self.records.values():
                if record.record_id in seen or record.record_id in self.record_targets:
                    continue
                if (
                    sum(other.alias == record.alias for other in self.records.values())
                    > 1
                ):
                    raise ThreadResolutionError("THREAD_DECISION_REQUIRED")
                groups.append(((record.record_id,), False))
            # Roll back allocation failure so a partial plan never leaks through.
            prior = (
                dict(self.threads),
                dict(self.record_targets),
                dict(self.message_targets),
                dict(self.message_participants),
                set(self.relation_unknown),
            )
            try:
                for ids, unknown in groups:
                    participants = frozenset().union(
                        *(self.records[key].participants for key in ids)
                    )
                    target = self._issue(participants)
                    for key in ids:
                        self.record_targets[key] = target
                        if unknown:
                            self.relation_unknown.add(key)
                        record = self.records[key]
                        for message_id in record.message_ids:
                            self.message_targets[message_id] = target
                            self.message_participants[message_id] = record.participants
            except Exception:
                (
                    self.threads,
                    self.record_targets,
                    self.message_targets,
                    self.message_participants,
                    self.relation_unknown,
                ) = prior
                raise

    def resolve(
        self,
        thread_id: str | None,
        caller: CallerEvidence,
        *,
        reply_to: int | None = None,
        new_thread: bool = False,
        label: str | None = None,
    ) -> str:
        agent_id = caller.require_agent()
        with self._lock:
            if (
                (new_thread and (thread_id is not None or reply_to is not None))
                or (reply_to is not None and thread_id is not None)
                or (
                    label is not None
                    and (thread_id is not None or reply_to is not None)
                )
            ):
                raise ThreadResolutionError("THREAD_INPUT_CONFLICT")
            if reply_to is not None:
                if type(
                    reply_to
                ) is not int or agent_id not in self.message_participants.get(
                    reply_to, frozenset()
                ):
                    raise ThreadResolutionError("MESSAGE_UNAVAILABLE")
                return self.message_targets[reply_to]
            if thread_id is None or new_thread:
                return self._issue(frozenset({agent_id}), label)
            if not isinstance(thread_id, str) or not validate_thread_id_format(
                thread_id
            ):
                raise ThreadResolutionError("INVALID_THREAD_ID")
            thread_id = thread_id.strip()
            if thread_id in self.threads:
                if agent_id not in self.threads[thread_id].participants:
                    raise ThreadResolutionError("THREAD_UNAVAILABLE")
                return thread_id
            records = [
                record
                for record in self.records.values()
                if record.alias == thread_id
                and agent_id in record.participants
                and (
                    not caller.legacy_sources or record.source in caller.legacy_sources
                )
            ]
            if any(record.record_id in self.relation_unknown for record in records):
                raise ThreadResolutionError("LEGACY_THREAD_UPDATE_REQUIRED")
            targets = {
                self.record_targets[record.record_id]
                for record in records
                if record.record_id in self.record_targets
            }
            if len(targets) == 1 and all(
                record.record_id in self.record_targets for record in records
            ):
                return next(iter(targets))
            if len(targets) > 1:
                raise ThreadResolutionError("LEGACY_THREAD_AMBIGUOUS", len(targets))
            raise ThreadResolutionError("LEGACY_THREAD_UNKNOWN")

    def record_delivery(
        self, thread_id: str, message_id: int, participants: frozenset[int]
    ) -> None:
        """Record a trusted backend delivery after its contact/owner checks."""
        with self._lock:
            if (
                thread_id not in self.threads
                or type(message_id) is not int
                or message_id <= 0
                or not participants
                or any(type(key) is not int or key <= 0 for key in participants)
            ):
                raise ThreadResolutionError("INVALID_THREAD_DELIVERY")
            if message_id in self.message_targets:
                if (
                    self.message_targets[message_id] == thread_id
                    and self.message_participants[message_id] == participants
                ):
                    return
                raise ThreadResolutionError("MESSAGE_ALREADY_MAPPED")
            current = self.threads[thread_id]
            self.threads[thread_id] = CanonicalThread(
                thread_id, current.participants | participants, current.label
            )
            self.message_targets[message_id] = thread_id
            self.message_participants[message_id] = participants

    @classmethod
    def from_export(cls, records: Iterable[LegacyThread], state: dict) -> ThreadCatalog:
        """Restore IDs from a validated receipt instead of reissuing on retry."""
        catalog = cls(records)
        try:
            if (
                set(state)
                != {
                    "records_digest",
                    "threads",
                    "record_targets",
                    "message_targets",
                    "message_participants",
                    "relation_unknown",
                }
                or state["records_digest"] != catalog._records_digest()
            ):
                raise ValueError
            aliases = {record.alias for record in catalog.records.values()}
            for item in state["threads"]:
                key = item["thread_id"]
                value = key.removeprefix("th-")
                participants = frozenset(item["participants"])
                label = item["label"]
                if (
                    not key.startswith("th-")
                    or len(value) != 32
                    or any(char not in "0123456789abcdef" for char in value)
                    or key in catalog.threads
                    or key in aliases
                ):
                    raise ValueError
                if (
                    not participants
                    or any(
                        type(agent) is not int or agent <= 0 for agent in participants
                    )
                    or (
                        label is not None
                        and (not isinstance(label, str) or len(label) > 128)
                    )
                ):
                    raise ValueError
                catalog.threads[key] = CanonicalThread(key, participants, label)
            catalog.record_targets = {
                int(key): value for key, value in state["record_targets"].items()
            }
            catalog.message_targets = {
                int(key): value for key, value in state["message_targets"].items()
            }
            catalog.message_participants = {
                int(key): frozenset(value)
                for key, value in state["message_participants"].items()
            }
            catalog.relation_unknown = set(state["relation_unknown"])
            if set(catalog.record_targets) - set(
                catalog.records
            ) or catalog.relation_unknown - set(catalog.record_targets):
                raise ValueError
            if any(
                target not in catalog.threads
                for target in (
                    *catalog.record_targets.values(),
                    *catalog.message_targets.values(),
                )
            ):
                raise ValueError
            if set(catalog.message_targets) != set(catalog.message_participants):
                raise ValueError
            for key, target in catalog.record_targets.items():
                record = catalog.records[key]
                if not record.participants <= catalog.threads[target].participants:
                    raise ValueError
                for message_id in record.message_ids:
                    if (
                        catalog.message_targets.get(message_id) != target
                        or catalog.message_participants.get(message_id)
                        != record.participants
                    ):
                        raise ValueError
            for message_id, participants in catalog.message_participants.items():
                if (
                    message_id <= 0
                    or not participants
                    or not participants
                    <= catalog.threads[catalog.message_targets[message_id]].participants
                ):
                    raise ValueError
            return catalog
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ThreadResolutionError("INVALID_THREAD_RECEIPT") from None

    def export(self) -> dict:
        with self._lock:
            return {
                "records_digest": self._records_digest(),
                "threads": [
                    {
                        "thread_id": item.thread_id,
                        "participants": sorted(item.participants),
                        "label": item.label,
                    }
                    for item in sorted(
                        self.threads.values(), key=lambda row: row.thread_id
                    )
                ],
                "record_targets": {
                    str(key): value
                    for key, value in sorted(self.record_targets.items())
                },
                "message_targets": {
                    str(key): value
                    for key, value in sorted(self.message_targets.items())
                },
                "message_participants": {
                    str(key): sorted(value)
                    for key, value in sorted(self.message_participants.items())
                },
                "relation_unknown": sorted(self.relation_unknown),
            }

    def _records_digest(self) -> str:
        rows = [
            {
                "record_id": record.record_id,
                "source": record.source,
                "alias": record.alias,
                "participants": sorted(record.participants),
                "message_ids": sorted(record.message_ids),
            }
            for record in sorted(self.records.values(), key=lambda row: row.record_id)
        ]
        raw = json.dumps(rows, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
