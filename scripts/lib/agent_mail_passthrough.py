#!/usr/bin/env python3
"""Teach an agent-mail checkout to accept the names it is given.

agent-mail decides whether a caller-supplied agent name survives registration,
and it decides differently depending on which version is running:

* Old servers strip ``-`` before any check, so ``Zesty-Einstein`` and
  ``ZestyEinstein`` are the same string, and both must appear in that server's
  adjective+noun table or the name is replaced by a generated one.
* Servers carrying #140 honour any name containing ``-``, ``_`` or ``.`` as an
  explicit identity, which is why this stack asks for hyphenated names.

Neither shape passes everywhere, and a name that is quietly replaced is worse
than one that is refused: the agent keeps running under a name nobody else can
address. ``AGENT_NAME_ENFORCEMENT_MODE=passthrough`` removes the question by
accepting whatever sanitised name it is handed, and it is four lines. This
applies those lines.

The mode is **opt-in**: every edit here sits behind ``mode == "passthrough"``,
so a patched server that was never told to use the mode takes exactly the paths
it took before. That is what makes it safe to offer for a server this installer
did not create.

Edits are matched exactly. A checkout whose text does not match is reported as
unsupported rather than patched approximately — a patch that lands in roughly
the right place is how a server starts behaving in a way nobody can reproduce.

Usage:
    agent_mail_passthrough.py --mail-dir DIR [--apply] [--result-json PATH]
    agent_mail_passthrough.py --mail-dir DIR --name-capability [--mail-env PATH]

Without ``--apply`` nothing is written and the verdict is printed, which is what
``--dry-run`` installs use.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import NamedTuple


ENV_KEY = "AGENT_NAME_ENFORCEMENT_MODE"
ENV_VALUE = "passthrough"


class PatchError(RuntimeError):
    """The checkout cannot be patched, and must be left exactly as it is."""


class Edit(NamedTuple):
    """One exact replacement."""

    relative_path: str
    before: str
    after: str


# Whether the mode is already available is a separate question from whether
# this exact patch was applied: a fork may have reached the same behaviour by
# other lines. Detection is therefore by meaning, not by our own text — the
# alternative is reporting "unsupported" for a checkout that plainly works.
APPLIED_MARKERS: dict[str, tuple[str, int]] = {
    # Membership in the allowed set, not the bare word: a comment or a docstring
    # mentioning the mode says nothing about whether the server accepts it.
    "src/mcp_agent_mail/config.py": ('"always_auto", "passthrough"', 1),
    "src/mcp_agent_mail/app.py": (
        'mode == "passthrough" or validate_agent_name_format(sanitized)',
        2,
    ),
}


def _applied_in(relative_path: str, text: str) -> bool:
    marker, needed = APPLIED_MARKERS[relative_path]
    return text.count(marker) >= needed


def _partially_applied_in(relative_path: str, text: str) -> bool:
    marker, _needed = APPLIED_MARKERS[relative_path]
    return marker in text


EDITS: tuple[Edit, ...] = (
    # The enum is fail-closed: an unknown mode raises at startup rather than
    # falling back to a default. So the name has to be allowed before it can
    # be selected, and this edit alone changes no behaviour at all.
    Edit(
        "src/mcp_agent_mail/config.py",
        'allowed=frozenset({"strict", "coerce", "always_auto"}),',
        'allowed=frozenset({"strict", "coerce", "always_auto", "passthrough"}),',
    ),
    # Name resolution when the caller supplies a hint that is not an explicit
    # identity. Without passthrough the name must be in the adjective+noun
    # table; with it, the sanitised form is taken as given.
    Edit(
        "src/mcp_agent_mail/app.py",
        "                if validate_agent_name_format(sanitized):\n"
        "                    if not await available(sanitized):",
        '                if mode == "passthrough" or validate_agent_name_format(sanitized):\n'
        "                    if not await available(sanitized):",
    ),
    # The same decision on the registration path.
    Edit(
        "src/mcp_agent_mail/app.py",
        "            elif validate_agent_name_format(sanitized):\n"
        "                desired_name = sanitized",
        '            elif mode == "passthrough" or validate_agent_name_format(sanitized):\n'
        "                desired_name = sanitized",
    ),
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply the agent-mail passthrough naming patch to a checkout."
    )
    parser.add_argument("--mail-dir", required=True, help="agent-mail checkout root")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the edits; without it, only report what would happen",
    )
    parser.add_argument("--result-json", help="write a machine-readable verdict here")
    parser.add_argument(
        "--name-capability",
        action="store_true",
        help="classify requested-name handling without registering a probe identity",
    )
    parser.add_argument(
        "--mail-env",
        help="agent-mail .env used to resolve AGENT_NAME_ENFORCEMENT_MODE",
    )
    return parser.parse_args(argv)


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PatchError(f"not an agent-mail checkout: {path} is missing") from exc
    except OSError as exc:
        raise PatchError(f"cannot read {path}: {exc}") from exc


def inspect(mail_dir: pathlib.Path) -> dict:
    """Classify the checkout without touching it.

    ``state`` is one of:
      ``already``      every edit is present
      ``applicable``   every edit matches exactly once and none is present
      ``unsupported``  the text does not match this version of the patch
      ``partial``      some edits are present and others are not
    """
    if not mail_dir.is_dir():
        raise PatchError(f"agent-mail directory does not exist: {mail_dir}")

    texts = {
        relative_path: _read(mail_dir / relative_path) for relative_path in APPLIED_MARKERS
    }

    present = [path for path, text in texts.items() if _applied_in(path, text)]
    traces = [path for path, text in texts.items() if _partially_applied_in(path, text)]
    missing: list[str] = []
    unmatched: list[str] = []

    for edit in EDITS:
        text = texts[edit.relative_path]
        if _applied_in(edit.relative_path, text):
            continue
        count = text.count(edit.before)
        if count == 1:
            missing.append(edit.relative_path)
        else:
            # Zero means a different version. More than one means the anchor is
            # not unique, and picking one occurrence would be a guess.
            unmatched.append(f"{edit.relative_path} (found {count} matches)")

    if len(present) == len(texts):
        state = "already"
    elif traces:
        # Some of it is there. Completing it would mean guessing which lines the
        # other patch meant to cover.
        state = "partial"
    elif unmatched:
        state = "unsupported"
    else:
        state = "applicable"

    return {
        "state": state,
        "mail_dir": str(mail_dir),
        "present": present,
        "missing": missing,
        "unmatched": unmatched,
        "env_key": ENV_KEY,
        "env_value": ENV_VALUE,
    }


def _enforcement_mode(
    mail_dir: pathlib.Path, mail_env: pathlib.Path | None
) -> tuple[str | None, str | None]:
    """Return the configured mode and an error, without consulting this process.

    The installer may be running under a different environment from the
    agent-mail service.  Reading our own ``os.environ`` here would turn that
    difference into a false claim about the server.
    """
    path = mail_env if mail_env is not None else mail_dir / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return "coerce", None
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() == ENV_KEY:
            mode = value.strip().strip("'\"").lower()
            return (mode or None), None if mode else f"{ENV_KEY} is empty"
    return "coerce", None


def _capability(
    mail_dir: pathlib.Path,
    *,
    status: str,
    evidence: str,
    mode: str | None,
    warning: str | None,
    detail: str,
) -> dict:
    return {
        "status": status,
        "evidence": evidence,
        "enforcement_mode": mode or "unknown",
        "mail_dir": str(mail_dir),
        "detail": detail,
        "warning": warning,
    }


def inspect_name_capability(
    mail_dir: pathlib.Path, *, mail_env: pathlib.Path | None = None
) -> dict:
    """Classify whether this checkout honours the stack's hyphenated names.

    This is deliberately passive.  A disposable ``register_agent`` call would
    answer the question, but it would also leave a durable garbage identity in
    the user's server.  Positive answers therefore require readable source and
    one of two known code paths: #140's explicit-identity validator, or the
    passthrough patch with its mode selected.  Anything unfamiliar is
    ``unknown`` rather than optimistic.
    """
    mail_dir = pathlib.Path(mail_dir)
    app_path = mail_dir / "src" / "mcp_agent_mail" / "app.py"
    try:
        app_text = _read(app_path)
    except PatchError as exc:
        return _capability(
            mail_dir,
            status="unknown",
            evidence="source-unreadable",
            mode=None,
            warning="requested-name handling is unknown because agent-mail source is unreadable",
            detail=str(exc),
        )

    mode, mode_error = _enforcement_mode(mail_dir, mail_env)
    if mode_error or mode not in {"strict", "coerce", "always_auto", "passthrough"}:
        return _capability(
            mail_dir,
            status="unknown",
            evidence="mode-unknown",
            mode=mode,
            warning="requested-name handling is unknown because the enforcement mode is unreadable or unsupported",
            detail=mode_error or f"unsupported {ENV_KEY}={mode}",
        )

    if mode == "always_auto":
        return _capability(
            mail_dir,
            status="replaced",
            evidence="always-auto",
            mode=mode,
            warning="requested names will be replaced by generated names",
            detail=f"{ENV_KEY}=always_auto ignores caller-supplied names",
        )

    # A configured passthrough value is only positive evidence when the source
    # accepts and implements it.  Otherwise that value may make the server fail
    # at startup; #140 elsewhere in app.py must not hide the mismatch.
    if mode == "passthrough":
        try:
            patch_state = inspect(mail_dir)["state"]
        except PatchError as exc:
            return _capability(
                mail_dir,
                status="unknown",
                evidence="source-unreadable",
                mode=mode,
                warning="requested-name handling is unknown because agent-mail source is unreadable",
                detail=str(exc),
            )
        if patch_state == "already":
            return _capability(
                mail_dir,
                status="honored",
                evidence="passthrough",
                mode=mode,
                warning=None,
                detail="passthrough patch is present and its mode is selected",
            )
        return _capability(
            mail_dir,
            status="unknown",
            evidence="passthrough-unavailable",
            mode=mode,
            warning="requested-name handling is unknown because passthrough is configured but unavailable",
            detail=f"passthrough inspection state: {patch_state}",
        )

    # Calls, rather than a comment or import, are the evidence that #140 is on
    # the registration path.  One call is enough across versions; the pinned
    # checkout currently has two.
    if "validate_explicit_agent_id(" in app_text:
        return _capability(
            mail_dir,
            status="honored",
            evidence="validate_explicit_agent_id",
            mode=mode,
            warning=None,
            detail="#140 explicit identity validation is present",
        )

    try:
        patch_state = inspect(mail_dir)["state"]
    except PatchError as exc:
        return _capability(
            mail_dir,
            status="unknown",
            evidence="source-unreadable",
            mode=mode,
            warning="requested-name handling is unknown because agent-mail source is unreadable",
            detail=str(exc),
        )

    if patch_state in {"applicable", "already"} and mode in {"coerce", "strict"}:
        return _capability(
            mail_dir,
            status="replaced",
            evidence="legacy-naming",
            mode=mode,
            warning="requested names will be replaced by generated names",
            detail="known legacy naming path lacks explicit identity support",
        )

    return _capability(
        mail_dir,
        status="unknown",
        evidence="source-unrecognised",
        mode=mode,
        warning="requested-name handling is unknown because the naming source is not recognised",
        detail=f"passthrough inspection state: {patch_state}",
    )


def apply(mail_dir: pathlib.Path) -> dict:
    """Apply every edit, or none of them."""
    verdict = inspect(mail_dir)
    if verdict["state"] == "already":
        return verdict
    if verdict["state"] != "applicable":
        raise PatchError(
            f"agent-mail at {mail_dir} does not match the passthrough patch "
            f"({verdict['state']}): "
            + "; ".join(verdict["unmatched"] or verdict["present"])
        )

    # Stage every file first. A half-patched checkout would start and then fail
    # in a way that looks like an agent-mail bug. Two edits share app.py, so
    # they accumulate into one staged text rather than each writing the file.
    staged: dict[pathlib.Path, str] = {}
    for edit in EDITS:
        path = mail_dir / edit.relative_path
        text = staged.get(path)
        if text is None:
            text = _read(path)
        if text.count(edit.before) != 1:
            raise PatchError(f"anchor is no longer unique in {edit.relative_path}")
        staged[path] = text.replace(edit.before, edit.after, 1)

    for path, text in staged.items():
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise PatchError(f"cannot write {path}: {exc}") from exc

    result = inspect(mail_dir)
    if result["state"] != "already":
        raise PatchError(f"patch did not take effect at {mail_dir}")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mail_dir = pathlib.Path(args.mail_dir).expanduser()
    if args.name_capability:
        if args.apply:
            raise SystemExit("--name-capability and --apply are mutually exclusive")
        mail_env = pathlib.Path(args.mail_env).expanduser() if args.mail_env else None
        verdict = inspect_name_capability(mail_dir, mail_env=mail_env)
        if args.result_json:
            pathlib.Path(args.result_json).write_text(
                json.dumps(verdict, indent=2) + "\n", encoding="utf-8"
            )
        print(json.dumps(verdict, separators=(",", ":"), sort_keys=True))
        return 0
    try:
        verdict = apply(mail_dir) if args.apply else inspect(mail_dir)
    except PatchError as exc:
        print(f"agent-mail passthrough patch: {exc}", file=sys.stderr)
        if args.result_json:
            pathlib.Path(args.result_json).write_text(
                json.dumps({"state": "error", "error": str(exc)}, indent=2) + "\n",
                encoding="utf-8",
            )
        return 1

    if args.result_json:
        pathlib.Path(args.result_json).write_text(
            json.dumps(verdict, indent=2) + "\n", encoding="utf-8"
        )
    print(verdict["state"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
