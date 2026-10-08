"""Explicit candidate proxy/hook/CLI adapters sharing one path contract.

These adapters do not discover identities, register agents, read credentials or
activate a mode. The existing bound proxy supplies its authenticated resolver;
the later hook cutover supplies a session binding resolver. The CLI emits only
normalized paths, not credentials or live requests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .reservation_paths import ReservationError, normalize_paths

OPERATIONS = frozenset(
    {
        "reserve_files",
        "renew_reservations",
        "release_reservations",
        "check_reservations",
    }
)


@dataclass(frozen=True)
class Binding:
    name: str
    token: str = field(repr=False)


class ReservationClient:
    """Relative paths use this observed session cwd, never project_key."""

    def __init__(
        self,
        resolve_binding: Callable[[], Binding],
        dispatch: Callable[[str, Mapping[str, Any]], Any],
        *,
        cwd: Path,
        wsl_drives: Mapping[str, str] | None = None,
    ):
        if not cwd.is_absolute():
            raise ReservationError("ABSOLUTE_CWD_REQUIRED")
        self.resolve_binding = resolve_binding
        self.dispatch = dispatch
        self.cwd = cwd
        self.wsl_drives = wsl_drives

    def call(self, operation: str, **arguments: Any) -> Any:
        if operation not in OPERATIONS:
            raise ReservationError("UNKNOWN_RESERVATION_OPERATION")
        if {"agent_name", "registration_token", "agent_id", "owner"} & arguments.keys():
            raise ReservationError("BOUND_IDENTITY_REQUIRED")
        binding = self.resolve_binding()
        if not binding.name or not binding.token:
            raise ReservationError("BOUND_IDENTITY_REQUIRED")
        args = dict(arguments)
        args.pop("project_key", None)
        if args.get("paths") is not None:
            args["paths"] = [
                path.value
                for path in normalize_paths(
                    args["paths"], anchor=self.cwd, wsl_drives=self.wsl_drives
                )
            ]
        return self.dispatch(
            operation,
            {**args, "agent_name": binding.name, "registration_token": binding.token},
        )


def hook_guard(
    document: Mapping[str, Any],
    *,
    cwd: Path,
    resolve_session: Callable[[str], Binding | None],
    dispatch: Callable[[str, Mapping[str, Any]], Any],
) -> int:
    """Candidate PreToolUse: unmanaged noop, bound failures always block.

    A resolver must distinguish genuinely unbound (None) from unavailable or
    ambiguous binding (exception). Protected roots are intentionally irrelevant.
    Return 2 mirrors Claude's blocking hook contract. No automatic registration.
    """
    try:
        session = document.get("session_id")
        if not isinstance(session, str) or not session:
            return 2
        binding = resolve_session(session)
        if binding is None:
            return 0
        tool_input = document.get("tool_input", {})
        path = tool_input.get("file_path", tool_input.get("path"))
        if path is None:
            return 0
        client = ReservationClient(lambda: binding, dispatch, cwd=cwd)
        return 0 if client.call("check_reservations", paths=[path]) is True else 2
    except Exception:
        # Do not turn transport/authentication/normalization failure into noop.
        return 2


def hook_release(
    document: Mapping[str, Any],
    *,
    cwd: Path,
    resolve_session: Callable[[str], Binding | None],
    dispatch: Callable[[str, Mapping[str, Any]], Any],
) -> int:
    if not _tool_succeeded(document):
        return 0
    try:
        session = document.get("session_id")
        if not isinstance(session, str) or not session:
            return 2
        binding = resolve_session(session)
        if binding is None:
            return 0
        tool_input = document.get("tool_input", {})
        path = tool_input.get("file_path", tool_input.get("path"))
        if path is None:
            return 0
        ReservationClient(lambda: binding, dispatch, cwd=cwd).call(
            "release_reservations", paths=[path]
        )
        return 0
    except Exception:
        return 2


_FAIL_PREFIX = re.compile(
    r"^(?:error:|pretooluse:|posttooluse:|blocked(?:\b|:)|permission denied\b)",
    re.IGNORECASE,
)


def _tool_succeeded(document: Mapping[str, Any]) -> bool:
    """Match the installed release hook's failed-result boundary."""

    def failed(value: Any) -> bool:
        if isinstance(value, dict):
            if (
                value.get("error") not in (None, "", False)
                or value.get("success") is False
            ):
                return True
            status = value.get("status")
            if isinstance(status, str) and status.lower() in {
                "error",
                "failed",
                "blocked",
            }:
                return True
            return any(failed(item) for item in value.values())
        if isinstance(value, list):
            return any(failed(item) for item in value)
        if isinstance(value, str):
            return bool(_FAIL_PREFIX.match(value.strip()))
        return False

    if not isinstance(document, Mapping):
        return False
    if any(
        failed({field: document.get(field)}) for field in ("error", "success", "status")
    ):
        return False
    return not any(
        failed(document.get(field))
        for field in ("tool_result", "tool_response", "tool_output")
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Normalize candidate absolute reservation paths; does not activate or contact Mail"
    )
    parser.add_argument(
        "--cwd", type=Path, help="explicit observed client cwd for relative paths"
    )
    parser.add_argument(
        "--wsl-drive", action="append", default=[], metavar="DRIVE=/mnt/drive"
    )
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    mappings = {}
    for value in args.wsl_drive:
        drive, separator, root = value.partition("=")
        if (
            not separator
            or len(drive) != 1
            or not drive.isascii()
            or not drive.isalpha()
            or not Path(root).is_absolute()
        ):
            parser.error("an explicit drive mapping must be DRIVE=/absolute/root")
        mappings[drive.lower()] = root
    try:
        paths = normalize_paths(args.paths, anchor=args.cwd, wsl_drives=mappings)
    except ReservationError as exc:
        parser.exit(2, str(exc) + "\n")
    json.dump(
        [{"kind": path.kind, "path": path.value} for path in paths],
        sys.stdout,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
