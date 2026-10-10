"""Explicit test transport for old adapter assertions, backed by real S2c SQL.

It translates test-only names and result shapes; it holds no lease state.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from agentstack_mail.global_s2c import S2cRuntime
from agentstack_mail.global_server import GlobalError
from agentstack_mail.reservation_paths import (
    ReservationError,
    ReservationPath,
    normalize_path,
)


class SQLTransport:
    def __init__(self, state, clock, identities=None):
        self.state, self.clock = state, clock
        self.identities = identities or {
            "alpha": (1, "test-alpha"),
            "beta": (2, "test-beta"),
        }
        self.runtime = S2cRuntime(state["config_path"])
        with self.runtime.transaction() as db:
            self.initial_max = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM file_reservations"
            ).fetchone()[0]

    @staticmethod
    def shape(row):
        try:
            path = normalize_path(row["path_pattern"])
        except ReservationError:
            path = ReservationPath("filesystem", row["path_pattern"])
        return SimpleNamespace(
            id=row["id"],
            owner=str(row["owner_agent_id"]),
            path=path,
            created=datetime.fromisoformat(row["created_ts"]).timestamp(),
            expires=datetime.fromisoformat(row["expires_ts"]).timestamp(),
            exclusive=row["exclusive"],
            reason=row["reason"],
            released=row["released_ts"] is not None,
            revision=row["revision"],
        )

    @property
    def _leases(self):
        from agentstack_mail.global_reservations import projection

        with self.runtime.transaction() as db:
            return {
                r["id"]: self.shape(projection(r))
                for r in db.execute(
                    "SELECT * FROM file_reservations WHERE id>?", (self.initial_max,)
                )
            }

    def dispatch(self, operation, arguments):
        args = dict(arguments)
        name, token = args.pop("agent_name", None), args.pop("registration_token", None)
        if name not in self.identities or token != self.identities[name][1]:
            raise ReservationError("AUTHENTICATION_REQUIRED")
        aid = self.identities[name][0]
        from test_global_server_s2b import owner

        tool = {
            "reserve_files": "file_reservation_paths",
            "renew_reservations": "renew_file_reservations",
            "release_reservations": "release_file_reservations",
            "check_reservations": "check_file_reservations",
        }.get(operation, operation)
        if "ids" in args:
            args["file_reservation_ids"] = args.pop("ids")
        args = owner(self.state, aid, **args)
        if tool != "check_file_reservations":
            args["request_id"] = str(uuid.uuid4())
        stamp = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        try:
            with (
                patch("agentstack_mail.global_s2b.now", return_value=stamp),
                patch("agentstack_mail.global_s2c.now", return_value=stamp),
            ):
                result = self.runtime.apply(tool, args)
        except GlobalError as exc:
            raise ReservationError(str(exc)) from None
        if tool == "check_file_reservations":
            return result["covered"]
        key = {
            "file_reservation_paths": "granted",
            "renew_file_reservations": "renewed",
            "release_file_reservations": "released",
        }[tool]
        return [self.shape(r) for r in result[key]]
