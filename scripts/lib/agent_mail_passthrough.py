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

Edits are matched exactly and every anchor must also be executable Python in
the expected AST shape. Comments and reference strings do not count. A
checkout whose code does not match is reported as unsupported rather than
patched approximately — a patch that lands in roughly the right place is how
a server starts behaving in a way nobody can reproduce.

Usage:
    agent_mail_passthrough.py --mail-dir DIR [--apply] [--result-json PATH]
    agent_mail_passthrough.py --mail-dir DIR --name-capability [--mail-env PATH]

Without ``--apply`` nothing is written and the verdict is printed, which is what
``--dry-run`` installs use.
"""

from __future__ import annotations

import argparse
import ast
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
APPLIED_REQUIRED: dict[str, int] = {
    "src/mcp_agent_mail/config.py": 1,
    "src/mcp_agent_mail/app.py": 2,
}


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_call(node: ast.AST, name: str, argument: str | None = None) -> bool:
    if not isinstance(node, ast.Call) or not _is_name(node.func, name):
        return False
    if argument is None:
        return True
    return len(node.args) == 1 and _is_name(node.args[0], argument)


def _is_mode_passthrough(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    if not isinstance(node.ops[0], ast.Eq) or len(node.comparators) != 1:
        return False
    left, right = node.left, node.comparators[0]
    return (
        _is_name(left, "mode")
        and isinstance(right, ast.Constant)
        and right.value == "passthrough"
    ) or (
        _is_name(right, "mode")
        and isinstance(left, ast.Constant)
        and left.value == "passthrough"
    )


def _is_passthrough_condition(node: ast.AST) -> bool:
    if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
        return False
    return len(node.values) == 2 and any(
        _is_mode_passthrough(value) for value in node.values
    ) and any(
        _is_call(value, "validate_agent_name_format", "sanitized")
        for value in node.values
    )


def _allowed_modes(node: ast.AST) -> set[str] | None:
    if not isinstance(node, ast.Call) or not _is_name(node.func, "frozenset"):
        return None
    if len(node.args) != 1 or not isinstance(node.args[0], (ast.Set, ast.Tuple)):
        return None
    values: set[str] = set()
    for element in node.args[0].elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        values.add(element.value)
    return values


def _is_name_mode_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not _is_name(node.func, "_enum"):
        return False
    return any(
        keyword.arg == "key"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value == ENV_KEY
        for keyword in node.keywords
    )


def _applied_count(relative_path: str, tree: ast.AST) -> int:
    if relative_path.endswith("config.py"):
        count = 0
        for node in ast.walk(tree):
            if not _is_name_mode_call(node):
                continue
            for keyword in node.keywords:
                modes = (
                    _allowed_modes(keyword.value)
                    if keyword.arg == "allowed"
                    else None
                )
                if modes and {"always_auto", "passthrough"} <= modes:
                    count += 1
        return count
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and _is_passthrough_condition(node.test)
    )


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, character in enumerate(text):
        if character == "\n":
            starts.append(index + 1)
    return starts


def _node_span(text: str, node: ast.AST) -> tuple[int, int] | None:
    location_fields = ("lineno", "col_offset", "end_lineno", "end_col_offset")
    if not all(hasattr(node, field) for field in location_fields):
        return None
    starts = _line_starts(text)
    try:
        start = starts[node.lineno - 1] + node.col_offset
        end = starts[node.end_lineno - 1] + node.end_col_offset
    except IndexError:
        return None
    return start, end


def _is_available_guard(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Await)
        and _is_call(node.test.operand.value, "available", "sanitized")
    )


def _is_desired_name_assignment(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and _is_name(node.targets[0], "desired_name")
        and _is_name(node.value, "sanitized")
    )


def _edit_spans(edit: Edit, text: str, tree: ast.AST) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    if edit.relative_path.endswith("config.py"):
        for node in ast.walk(tree):
            if not _is_name_mode_call(node):
                continue
            for keyword in node.keywords:
                if keyword.arg != "allowed" or _allowed_modes(keyword.value) != {
                    "strict", "coerce", "always_auto"
                }:
                    continue
                span = _node_span(text, keyword)
                if span:
                    start, end = span
                    # ast.keyword ends before the separating comma; the exact
                    # edit deliberately includes it so the rewritten call keeps
                    # its original layout.
                    candidate = (start, end + 1)
                    if text[slice(*candidate)] == edit.before:
                        spans.append(candidate)
        return spans

    wants_guard = edit.before.lstrip().startswith("if ")
    starts = _line_starts(text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not _is_call(
            node.test, "validate_agent_name_format", "sanitized"
        ):
            continue
        if not node.body:
            continue
        body_matches = (
            _is_available_guard(node.body[0])
            if wants_guard
            else _is_desired_name_assignment(node.body[0])
        )
        if not body_matches:
            continue
        try:
            start = starts[node.lineno - 1]
        except IndexError:
            continue
        end = start + len(edit.before)
        if text[start:end] == edit.before:
            spans.append((start, end))
    return spans


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
        relative_path: _read(mail_dir / relative_path) for relative_path in APPLIED_REQUIRED
    }
    trees: dict[str, ast.AST | None] = {}
    syntax_errors: list[str] = []
    for relative_path, text in texts.items():
        try:
            trees[relative_path] = ast.parse(text, filename=relative_path)
        except SyntaxError as exc:
            trees[relative_path] = None
            syntax_errors.append(
                f"{relative_path} (syntax error at line {exc.lineno or 'unknown'})"
            )

    applied_counts = {
        path: _applied_count(path, tree) if tree is not None else 0
        for path, tree in trees.items()
    }
    present = [
        path
        for path, count in applied_counts.items()
        if count >= APPLIED_REQUIRED[path]
    ]
    traces = [path for path, count in applied_counts.items() if count > 0]
    missing: list[str] = []
    unmatched: list[str] = list(syntax_errors)

    for edit in EDITS:
        text = texts[edit.relative_path]
        tree = trees[edit.relative_path]
        if applied_counts[edit.relative_path] >= APPLIED_REQUIRED[edit.relative_path]:
            continue
        count = len(_edit_spans(edit, text, tree)) if tree is not None else 0
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

    try:
        app_tree = ast.parse(app_text, filename=str(app_path))
    except SyntaxError as exc:
        return _capability(
            mail_dir,
            status="unknown",
            evidence="source-unrecognised",
            mode=mode,
            warning="requested-name handling is unknown because the naming source is not valid Python",
            detail=f"syntax error at line {exc.lineno or 'unknown'}",
        )

    # Calls, rather than a comment, import, or reference string, are the
    # evidence that #140 is on the registration path. One call is enough across
    # versions; the pinned checkout currently has two.
    if any(
        _is_call(node, "validate_explicit_agent_id") for node in ast.walk(app_tree)
    ):
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
    # in a way that looks like an agent-mail bug. Replacements use AST-confirmed
    # code spans, applied back-to-front so comments and reference strings remain
    # untouched and earlier edits cannot shift later offsets.
    source_texts: dict[pathlib.Path, str] = {}
    source_trees: dict[pathlib.Path, ast.AST] = {}
    replacements: dict[pathlib.Path, list[tuple[int, int, str]]] = {}
    for edit in EDITS:
        path = mail_dir / edit.relative_path
        text = source_texts.setdefault(path, _read(path))
        tree = source_trees.get(path)
        if tree is None:
            try:
                tree = ast.parse(text, filename=edit.relative_path)
            except SyntaxError as exc:
                raise PatchError(
                    f"cannot parse {edit.relative_path}: line {exc.lineno or 'unknown'}"
                ) from exc
            source_trees[path] = tree
        spans = _edit_spans(edit, text, tree)
        if len(spans) != 1:
            raise PatchError(f"anchor is no longer unique in {edit.relative_path}")
        start, end = spans[0]
        replacements.setdefault(path, []).append((start, end, edit.after))

    staged: dict[pathlib.Path, str] = {}
    for path, edits in replacements.items():
        text = source_texts[path]
        for start, end, replacement in sorted(edits, reverse=True):
            text = text[:start] + replacement + text[end:]
        staged[path] = text

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
