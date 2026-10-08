"""Candidate reply edges; no thread namespace or live DB/runtime wiring.

Caller and per-message sender/recipient facts come from a trusted collector.
The in-memory lock models atomic reply/purge; persistent FK/transaction and
operation-specific owner/contact checks are implemented by later PRs.
"""

from __future__ import annotations

import json
import re
import time
from uuid import uuid4
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Iterable

from .namespace_plan import fingerprint


class ReplyError(ValueError):
    """Fixed diagnostics without message contents or recipient identities."""


@dataclass(frozen=True, slots=True)
class CallerEvidence:
    agent_id: int | None
    confirmed: bool

    def require_agent(self) -> int:
        if self.confirmed is not True or not valid_id(self.agent_id):
            raise ReplyError("CALLER_UNCONFIRMED")
        return self.agent_id


def valid_id(value: object) -> bool:
    return type(value) is int and 0 < value <= 2**63 - 1


def numeric_legacy(value: str) -> int | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ReplyError("INVALID_LEGACY_THREAD")
    value = value.strip()
    if not re.fullmatch(r"[0-9]+", value):
        return None
    number = int(value)
    if not valid_id(number):
        raise ReplyError("INVALID_REPLY_ID")
    return number


@dataclass(frozen=True, slots=True)
class MessageRecord:
    message_id: int
    source: str
    sender_id: int
    to: tuple[int, ...] = ()
    cc: tuple[int, ...] = ()
    bcc: tuple[int, ...] = ()
    reply_to: int | None = None
    legacy_thread_label: str | None = None
    topic: str | None = None

    def permits(self, agent_id: int) -> bool:
        return agent_id == self.sender_id or agent_id in (*self.to, *self.cc, *self.bcc)

    @classmethod
    def from_legacy(cls, *, thread_id: str | None = None, **fields) -> MessageRecord:
        """Copy a numeric root reference, never infer an unrecorded direct parent."""
        parent = numeric_legacy(thread_id) if thread_id is not None else None
        label = thread_id.strip() if thread_id is not None and parent is None else None
        return cls(**fields, reply_to=parent, legacy_thread_label=label)


@dataclass(frozen=True, slots=True)
class ConversationView:
    message_ids: tuple[int, ...]
    incomplete: bool
    has_more: bool
    next_cursor: str | None = None
    scan_exhausted: bool = False


@dataclass(slots=True)
class _ReadState:
    query: tuple
    agent_id: int
    frames: list[tuple[int, int]]
    seen: set[int]
    position: int = 0
    incomplete: bool = False
    expires: float = 0


@dataclass(frozen=True, slots=True)
class PurgePlan:
    candidates: tuple[int, ...]
    delete_ids: tuple[int, ...]
    held_ids: tuple[int, ...]
    source_digest: str


class ReplyCatalog:
    """Explicit candidate data, with message-level checks and stable export."""

    def __init__(self, records: Iterable[MessageRecord] = ()):
        rows = tuple(records)
        self._records = {row.message_id: row for row in rows}
        if len(self._records) != len(rows):
            raise ReplyError("DUPLICATE_MESSAGE_ID")
        self._lock = RLock()
        self._validate()
        self._cursors: dict[str, _ReadState] = {}
        self._reindex()

    def _reindex(self) -> None:
        neighbors = {key: [] for key in self._records}
        labels: dict[tuple[str, str, int], list[int]] = {}
        sources: dict[tuple[str, int], set[str]] = {}
        for key, row in self._records.items():
            if row.reply_to is not None:
                neighbors[key].append(row.reply_to)
                neighbors[row.reply_to].append(key)
            if row.legacy_thread_label is not None:
                for agent in {row.sender_id, *row.to, *row.cc, *row.bcc}:
                    labels.setdefault(
                        (row.legacy_thread_label, row.source, agent), []
                    ).append(key)
                    sources.setdefault((row.legacy_thread_label, agent), set()).add(
                        row.source
                    )
        self._neighbors = {
            key: tuple(sorted(values)) for key, values in neighbors.items()
        }
        self._labels = {key: tuple(sorted(values)) for key, values in labels.items()}
        self._label_sources = {
            key: tuple(sorted(values)) for key, values in sources.items()
        }
        self._cursors.clear()  # Every mutation requires a fresh read snapshot.

    def _validate(self) -> None:
        for row in self._records.values():
            if (
                not valid_id(row.message_id)
                or not valid_id(row.sender_id)
                or not isinstance(row.source, str)
                or not row.source
                or any(
                    not isinstance(group, tuple) for group in (row.to, row.cc, row.bcc)
                )
                or any(not valid_id(key) for key in (*row.to, *row.cc, *row.bcc))
                or (
                    row.legacy_thread_label is not None
                    and (numeric_legacy(row.legacy_thread_label) is not None)
                )
                or (row.topic is not None and not isinstance(row.topic, str))
            ):
                raise ReplyError("INVALID_MESSAGE_RECORD")
            if row.reply_to is not None and (
                not valid_id(row.reply_to) or row.reply_to not in self._records
            ):
                raise ReplyError("DANGLING_REPLY")
        finished: set[int] = set()
        for key in self._records:
            path: set[int] = set()
            current = key
            while current is not None and current not in finished:
                if current in path:
                    raise ReplyError("REPLY_CYCLE")
                path.add(current)
                current = self._records[current].reply_to
            finished.update(path)

    def resolve(
        self,
        caller: CallerEvidence,
        *,
        reply_to: int | None = None,
        legacy_thread_id: str | None = None,
    ) -> int | None:
        agent = caller.require_agent()
        if reply_to is not None and not valid_id(reply_to):
            raise ReplyError("INVALID_REPLY_ID")
        with self._lock:
            if legacy_thread_id is not None:
                legacy = numeric_legacy(legacy_thread_id)
                if legacy is None:
                    raise ReplyError("LEGACY_THREAD_READ_ONLY")
                if reply_to is not None and reply_to != legacy:
                    raise ReplyError("REPLY_INPUT_CONFLICT")
                reply_to = legacy
            if reply_to is None:
                return None
            if not valid_id(reply_to):
                raise ReplyError("INVALID_REPLY_ID")
            parent = self._records.get(reply_to)
            if parent is None or not parent.permits(agent):
                raise ReplyError("MESSAGE_UNAVAILABLE")
            return reply_to

    def record_delivery(self, row: MessageRecord, caller: CallerEvidence) -> None:
        """Trusted backend calls after owner/contact validation, under one lock."""
        agent = caller.require_agent()
        with self._lock:
            if row.sender_id != agent or row.legacy_thread_label is not None:
                raise ReplyError("INVALID_NEW_MESSAGE")
            if row.message_id in self._records:
                if self._records[row.message_id] == row:
                    return
                raise ReplyError("MESSAGE_ALREADY_MAPPED")
            self.resolve(caller, reply_to=row.reply_to)
            self._records[row.message_id] = row
            try:
                self._validate()
            except Exception:
                del self._records[row.message_id]
                raise
            self._reindex()

    def _resume(self, cursor: str, agent: int) -> _ReadState:
        if not isinstance(cursor, str):
            raise ReplyError("INVALID_CURSOR")
        state = self._cursors.get(cursor)
        if (
            state is None
            or state.agent_id != agent
            or state.expires <= time.monotonic()
        ):
            raise ReplyError("INVALID_CURSOR")
        return state

    def _page(
        self, query: tuple, agent: int, limit: int, scan_limit: int, cursor: str | None
    ) -> ConversationView:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ReplyError("INVALID_LIMIT")
        if type(scan_limit) is not int or not 1 <= scan_limit <= 1000:
            raise ReplyError("INVALID_SCAN_LIMIT")
        if cursor is not None:
            state = self._resume(cursor, agent)
            if state.query != query:
                raise ReplyError("INVALID_CURSOR")
            self._cursors.pop(cursor)  # Single-use continuation, caller-bound.
        else:
            state = _ReadState(
                query,
                agent,
                [(query[1], -1)] if query[0] == "conversation" else [],
                set(),
            )
        output: list[int] = []
        steps = 0
        if query[0] == "conversation":
            # Indexed depth-first traversal. Each node/edge step consumes a
            # budget unit; even a high-degree node is expanded incrementally.
            while state.frames and steps < scan_limit and len(output) < limit:
                key, position = state.frames[-1]
                steps += 1
                if position == -1:
                    if key in state.seen:
                        state.frames.pop()
                        continue
                    state.seen.add(key)
                    state.frames[-1] = (key, 0)
                    if self._records[key].permits(agent):
                        output.append(key)
                    else:
                        state.incomplete = True
                elif position >= len(self._neighbors[key]):
                    state.frames.pop()
                else:
                    neighbor = self._neighbors[key][position]
                    state.frames[-1] = (key, position + 1)
                    if neighbor not in state.seen:
                        state.frames.append((neighbor, -1))
            pending = bool(state.frames)
        else:
            keys = self._labels[(query[1], query[2], agent)]
            while (
                state.position < len(keys)
                and steps < scan_limit
                and len(output) < limit
            ):
                key = keys[state.position]
                state.position += 1
                steps += 1
                if self._records[key].permits(agent):
                    output.append(key)
            pending = state.position < len(keys)
        next_cursor = None
        if pending:
            now = time.monotonic()
            self._cursors = {
                key: value
                for key, value in self._cursors.items()
                if value.expires > now
            }
            if len(self._cursors) >= 128:
                raise ReplyError("CURSOR_CAPACITY")
            next_cursor = uuid4().hex
            state.expires = now + 300
            self._cursors[next_cursor] = state
        return ConversationView(
            tuple(output),
            state.incomplete,
            pending,
            next_cursor,
            pending and steps >= scan_limit,
        )

    def conversation(
        self,
        message_id: int,
        caller: CallerEvidence,
        *,
        limit: int = 50,
        scan_limit: int = 1000,
        cursor: str | None = None,
    ) -> ConversationView:
        agent = caller.require_agent()
        if not valid_id(message_id):
            raise ReplyError("INVALID_REPLY_ID")
        with self._lock:
            self.resolve(caller, reply_to=message_id)
            return self._page(
                ("conversation", message_id), agent, limit, scan_limit, cursor
            )

    def legacy_read(
        self,
        value: str,
        caller: CallerEvidence,
        *,
        limit: int = 50,
        scan_limit: int = 1000,
        cursor: str | None = None,
    ) -> tuple[ConversationView, ...]:
        """Resume each returned view with its opaque cursor and the same input."""
        agent = caller.require_agent()
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise ReplyError("INVALID_LEGACY_THREAD")
        with self._lock:
            if cursor is not None:
                state = self._resume(cursor, agent)
                if state.query[-1] != value or state.query[0] not in {
                    "legacy_label",
                    "conversation",
                }:
                    raise ReplyError("INVALID_CURSOR")
                return (self._page(state.query, agent, limit, scan_limit, cursor),)
            results = []
            for item in value.split(","):
                number = numeric_legacy(item)
                if number is not None:
                    self.resolve(caller, reply_to=number)
                    results.append(
                        self._page(
                            ("conversation", number, value),
                            agent,
                            limit,
                            scan_limit,
                            None,
                        )
                    )
                else:
                    queries = [
                        ("legacy_label", item.strip(), source, value)
                        for source in self._label_sources.get((item.strip(), agent), ())
                    ]
                    if not queries:
                        raise ReplyError("MESSAGE_UNAVAILABLE")
                    for query in queries:
                        results.append(
                            self._page(query, agent, limit, scan_limit, None)
                        )
            return tuple(results)

    def topic_ids(self, topic: str, caller: CallerEvidence) -> tuple[int, ...]:
        agent = caller.require_agent()
        if not isinstance(topic, str) or not topic.strip():
            raise ReplyError("INVALID_TOPIC")
        with self._lock:
            return tuple(
                sorted(
                    key
                    for key, row in self._records.items()
                    if row.topic is not None
                    and row.topic.lower() == topic.strip().lower()
                    and row.permits(agent)
                )
            )

    def plan_purge(self, candidates: Iterable[int]) -> PurgePlan:
        with self._lock:
            selected = set(candidates)
            if any(not valid_id(key) or key not in self._records for key in selected):
                raise ReplyError("INVALID_PURGE_CANDIDATES")
            # Preserve every ancestor of every message outside the candidate
            # set. A held parent may itself need a held grandparent.
            keep = set(self._records) - selected
            pending = list(keep)
            while pending:
                parent = self._records[pending.pop()].reply_to
                if parent is not None and parent not in keep:
                    keep.add(parent)
                    pending.append(parent)
            return PurgePlan(
                tuple(sorted(selected)),
                tuple(sorted(selected - keep)),
                tuple(sorted(selected & keep)),
                fingerprint(self.export()),
            )

    def purge(self, candidates: Iterable[int]) -> PurgePlan:
        """Replan at execution, not from a stale dry-run; never cascade children."""
        with self._lock:
            plan = self.plan_purge(candidates)
            for key in plan.delete_ids:
                del self._records[key]
            self._validate()
            if plan.delete_ids:
                self._reindex()
            return plan

    def export(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "messages": [
                    asdict(self._records[key]) for key in sorted(self._records)
                ],
            }

    @classmethod
    def from_export(cls, state: dict) -> ReplyCatalog:
        try:
            if (
                set(state) != {"version", "messages"}
                or type(state["version"]) is not int
                or state["version"] != 1
            ):
                raise ValueError
            records = [
                MessageRecord(
                    **{
                        **row,
                        **{field: tuple(row[field]) for field in ("to", "cc", "bcc")},
                    }
                )
                for row in state["messages"]
            ]
            return cls(records)
        except (AttributeError, TypeError, KeyError, ValueError):
            raise ReplyError("INVALID_REPLY_RECEIPT") from None
