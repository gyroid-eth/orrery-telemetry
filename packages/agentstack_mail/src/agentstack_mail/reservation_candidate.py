"""Executable absolute-lease candidate; never installed as the default server.

PR2's isolated store is deliberately not a database migration. A later cutover
must supply persistent storage, instance binding and a verified lease import.
The authentication callback is mandatory; callers cannot supply owner IDs.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Any

from .reservation_activity import Activity, probe_activity
from .reservation_paths import (
    ReservationError,
    ReservationPath,
    normalize_paths,
    paths_overlap,
)


@dataclass(frozen=True)
class Lease:
    id: int
    owner: str
    path: ReservationPath
    created: float
    expires: float
    exclusive: bool
    reason: str
    released: bool = False
    revision: int = 0


class ReservationServer:
    """Single local namespace, atomic acquisition and owner-checked lifecycle.

    Explicitly constructed candidate only. ``authenticate`` validates the real
    token and returns a stable owner identity, or raises on failure. No token is
    stored in leases, diagnostics or replies. ``project_key`` is never an anchor.
    """

    def __init__(
        self,
        authenticate: Callable[[str, str | None], str],
        *,
        clock: Callable[[], float] = time.time,
        probe: Callable[[ReservationPath], Activity] = probe_activity,
        inactivity: float = 900,
        grace: float = 900,
    ):
        self.authenticate = authenticate
        self.clock = clock
        self.probe = probe
        self.inactivity = inactivity
        self.grace = grace
        self._lock = threading.RLock()
        self._leases: dict[int, Lease] = {}
        self._next_id = 1
        self._activity: dict[str, tuple[float, float | None, int]] = {}

    def _owner(self, name: str, token: str | None) -> str:
        owner = self.authenticate(name, token)
        if not isinstance(owner, str) or not owner:
            raise ReservationError("AUTHENTICATION_REQUIRED")
        return owner

    def _touch(self, owner: str, *, mail: bool = False) -> None:
        previous = self._activity.get(owner, (0, None, 0))
        now = self.clock()
        self._activity[owner] = (now, now if mail else previous[1], previous[2] + 1)

    def record_activity(
        self, name: str, token: str | None, *, mail: bool = False
    ) -> None:
        owner = self._owner(name, token)
        with self._lock:
            self._touch(owner, mail=mail)

    @staticmethod
    def _seconds(value: int) -> None:
        if type(value) is not int or not 60 <= value <= 86400:
            raise ReservationError("TTL_OUT_OF_RANGE")

    def acquire(
        self,
        name: str,
        token: str | None,
        paths: list[str],
        *,
        ttl_seconds: int = 3600,
        exclusive: bool = True,
        reason: str = "",
    ) -> list[Lease]:
        owner = self._owner(name, token)
        self._seconds(ttl_seconds)
        if (
            type(exclusive) is not bool
            or not isinstance(reason, str)
            or len(reason) > 500
        ):
            raise ReservationError("INVALID_LEASE_OPTIONS")
        normalized = normalize_paths(paths)
        with self._lock:
            now = self.clock()
            active = [
                lease
                for lease in self._leases.values()
                if not lease.released and lease.expires > now
            ]
            for path in normalized:
                for lease in active:
                    if (
                        lease.owner != owner
                        and (exclusive or lease.exclusive)
                        and paths_overlap(path, lease.path)
                    ):
                        raise ReservationError("RESERVATION_CONFLICT")
            result = []
            for path in normalized:
                own = next(
                    (
                        lease
                        for lease in active
                        if lease.owner == owner
                        and lease.path == path
                        and lease.exclusive == exclusive
                    ),
                    None,
                )
                if own is not None:
                    lease = replace(
                        own,
                        expires=max(own.expires, now + ttl_seconds),
                        revision=own.revision + 1,
                    )
                else:
                    lease = Lease(
                        self._next_id,
                        owner,
                        path,
                        now,
                        now + ttl_seconds,
                        exclusive,
                        reason,
                    )
                    self._next_id += 1
                    active.append(lease)
                self._leases[lease.id] = lease
                result.append(lease)
            self._touch(owner)
            return result

    def _selected(self, ids: list[int] | None, paths: list[str] | None) -> list[Lease]:
        if ids is not None and (
            not isinstance(ids, list)
            or len(ids) > 100
            or any(type(i) is not int or i <= 0 for i in ids)
        ):
            raise ReservationError("INVALID_LEASE_IDS")
        normalized = normalize_paths(paths) if paths is not None else []
        # Lifecycle path selectors select exact normalized patterns, not every
        # overlapping reservation. Missing IDs are rejected atomically.
        if ids and any(i not in self._leases for i in ids):
            raise ReservationError("LEASE_NOT_FOUND")
        return [
            lease
            for lease in self._leases.values()
            if (not ids and not normalized)
            or lease.id in (ids or [])
            or any(
                lease.path == path
                or (
                    not lease.path.is_glob
                    and not path.is_glob
                    and paths_overlap(lease.path, path)
                )
                for path in normalized
            )
        ]

    def renew(
        self,
        name: str,
        token: str | None,
        *,
        ids: list[int] | None = None,
        paths: list[str] | None = None,
        extend_seconds: int = 1800,
    ) -> list[Lease]:
        owner = self._owner(name, token)
        self._seconds(extend_seconds)
        with self._lock:
            selected = self._selected(ids, paths)
            if any(lease.owner != owner for lease in selected if ids or paths):
                raise ReservationError("LEASE_OWNER_REQUIRED")
            now = self.clock()
            selected = [
                lease
                for lease in selected
                if lease.owner == owner and not lease.released and lease.expires > now
            ]
            result = [
                replace(
                    lease,
                    expires=lease.expires + extend_seconds,
                    revision=lease.revision + 1,
                )
                for lease in selected
            ]
            self._leases.update({lease.id: lease for lease in result})
            if result:
                self._touch(owner)
            return result

    def release(
        self,
        name: str,
        token: str | None,
        *,
        ids: list[int] | None = None,
        paths: list[str] | None = None,
    ) -> list[Lease]:
        owner = self._owner(name, token)
        with self._lock:
            selected = self._selected(ids, paths)
            if any(lease.owner != owner for lease in selected if ids or paths):
                raise ReservationError("LEASE_OWNER_REQUIRED")
            result = [
                replace(lease, released=True, revision=lease.revision + 1)
                for lease in selected
                if lease.owner == owner and not lease.released
            ]
            self._leases.update({lease.id: lease for lease in result})
            self._touch(owner)
            return result

    def check(self, name: str, token: str | None, paths: list[str]) -> bool:
        owner = self._owner(name, token)
        normalized = normalize_paths(paths)
        if any(path.is_glob for path in normalized):
            raise ReservationError("CHECK_REQUIRES_CONCRETE_PATH")
        with self._lock:
            now = self.clock()
            self._touch(owner)
            return all(
                any(
                    lease.owner == owner
                    and not lease.released
                    and lease.expires > now
                    and paths_overlap(lease.path, path)
                    for lease in self._leases.values()
                )
                for path in normalized
            )

    def collect(self, ids: list[int]) -> dict[int, str]:
        """Bounded internal GC batch; unknown cannot extend TTL or prove stale.

        Snapshot/revision checks protect renew/release/Mail activity during a
        probe. A second complete probe prevents reuse of old filesystem evidence.
        No filesystem transaction can exclude a write after the final stat.
        """
        if not ids or len(ids) > 100 or any(type(i) is not int for i in ids):
            raise ReservationError("BOUNDED_LEASE_IDS_REQUIRED")
        with self._lock:
            candidates = [
                (
                    self._leases[i],
                    self._activity.get(self._leases[i].owner, (0, None, 0)),
                )
                for i in ids
                if i in self._leases
            ]
        result = {}
        for lease, activity in candidates:
            observed = (
                self.probe(lease.path)
                if lease.expires > self.clock() and not lease.released
                else Activity(True)
            )
            if self._stale(lease, activity, observed, self.clock()):
                observed = self.probe(lease.path)
            with self._lock:
                current = self._leases[lease.id]
                latest = self._activity.get(lease.owner, (0, None, 0))
                now = self.clock()
                if current.released:
                    reason = "released"
                elif current.expires <= now:
                    reason = "ttl_expired"
                elif current != lease or latest != activity:
                    reason = "activity_changed"
                elif not observed.complete:
                    reason = "activity_unknown"
                elif self._stale(current, latest, observed, now):
                    reason = "stale"
                else:
                    reason = "active"
                if reason in {"ttl_expired", "stale"}:
                    self._leases[lease.id] = replace(
                        current, released=True, revision=current.revision + 1
                    )
                result[lease.id] = reason
        return result

    def _stale(
        self,
        lease: Lease,
        activity: tuple[float, float | None, int],
        probe: Activity,
        now: float,
    ) -> bool:
        agent, mail, _ = activity
        return (
            probe.complete
            and not lease.released
            and lease.expires > now
            and agent <= now - self.inactivity
            and (mail is None or mail <= now - self.grace)
            and (probe.filesystem is None or probe.filesystem <= now - self.grace)
            and (probe.git is None or probe.git <= now - self.grace)
            and (
                probe.matched
                or lease.path.kind == "virtual"
                or lease.created <= now - self.grace
            )
        )

    def dispatch(self, operation: str, arguments: Mapping[str, Any]) -> Any:
        """Candidate server boundary, accepting but ignoring old project scope."""
        args = dict(arguments)
        args.pop("project_key", None)
        name = args.pop("agent_name", "")
        token = args.pop("registration_token", None)
        if "file_reservation_ids" in args:
            args["ids"] = args.pop("file_reservation_ids")
        # Raw candidate tool names and the bound proxy's narrow names share
        # the same lifecycle; rendering/context-manager macros remain separate.
        operation = {
            "file_reservation_paths": "reserve_files",
            "renew_file_reservations": "renew_reservations",
            "release_file_reservations": "release_reservations",
        }.get(operation, operation)
        requested_format = args.pop("format", None)
        if requested_format not in {None, "json"}:
            raise ReservationError("UNSUPPORTED_CANDIDATE_FORMAT")
        routes = {
            "reserve_files": self.acquire,
            "renew_reservations": self.renew,
            "release_reservations": self.release,
            "check_reservations": self.check,
        }
        if operation not in routes:
            raise ReservationError("UNKNOWN_RESERVATION_OPERATION")
        return routes[operation](name, token, **args)
