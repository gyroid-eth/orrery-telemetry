#!/usr/bin/env python3
"""Read one protection setting as literal data, never as executable shell.

Only the single-line export syntax emitted by shlex.quote (and ordinary
literal shell quoting) is accepted. Absence, empty, value and invalid are
separate states: an invalid file must never look like permission to reset it.
"""
from __future__ import annotations

import pathlib
import re
import shlex
import sys


class InvalidAssignment(ValueError):
    pass


def literal_value(raw: str, *, allow_multiline: bool = False) -> str:
    value: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\0" or (ch in "\r\n" and not allow_multiline):
            raise InvalidAssignment("multiline/control-character assignment")
        if ch in " \t":
            rest = raw[i:].lstrip(" \t")
            if not rest or rest.startswith("#"):
                break
            raise InvalidAssignment("multiple words or shell commands")
        if ch == "'":
            end = raw.find("'", i + 1)
            if end < 0:
                raise InvalidAssignment("unterminated or multiline single quote")
            value.append(raw[i + 1:end])
            i = end + 1
            continue
        if ch == '"':
            i += 1
            while i < len(raw) and raw[i] != '"':
                ch = raw[i]
                if ch in "$`":
                    raise InvalidAssignment("expansion inside double quotes")
                if ch == "\\":
                    i += 1
                    if i == len(raw):
                        raise InvalidAssignment("trailing backslash")
                    if raw[i] not in '\\"$`':
                        value.append("\\")
                    ch = raw[i]
                value.append(ch)
                i += 1
            if i == len(raw):
                raise InvalidAssignment("unterminated or multiline double quote")
            i += 1
            continue
        if ch == "\\":
            i += 1
            if i == len(raw):
                raise InvalidAssignment("trailing backslash or line continuation")
            value.append(raw[i])
        elif ch in "$`;|&<>(){}*?[]~":
            raise InvalidAssignment("expansion or shell operator")
        else:
            value.append(ch)
        i += 1
    result = "".join(value)
    if "\0" in result or (not allow_multiline and any(ch in result for ch in "\r\n")):
        raise InvalidAssignment("multiline/control-character value")
    return result


def read_assignment(path: pathlib.Path, name: str) -> tuple[str, str]:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        raise InvalidAssignment("invalid setting name")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "absent", ""
    except (OSError, UnicodeError) as error:
        raise InvalidAssignment(f"cannot read installed settings: {error}") from error
    # Legacy files can contain unrelated shell logic. Quoted/escaped setting
    # names still count as protection declarations and must fail strict parsing.
    try:
        shlex.split(raw, comments=True, posix=True)
    except ValueError as error:
        raise InvalidAssignment("invalid shell quoting") from error
    # Even without a literal protection name, a computed export could assign
    # one. Require literal export names throughout, but leave values for
    # unrelated settings to their own validators (e.g. Mail env evidence).
    # env.sh is generated data, not a shell program. Track logical records so
    # an apparent export inside another setting's multiline quote is never
    # mistaken for a protection assignment. Other generated multiline values
    # (e.g. model lists) are fine; multiline protection values are not.
    records: list[str] = []
    pending = ""
    for line in raw.splitlines(keepends=True):
        if not pending and (not line.strip() or line.lstrip().startswith("#")):
            continue
        pending += line
        try:
            shlex.split(pending, comments=True, posix=True)
        except ValueError:
            continue
        records.append(pending.rstrip("\n"))
        pending = ""
    if pending:
        records.append(pending)
    assignment = re.compile(r"^[ \t]*export[ \t]+([A-Z][A-Z0-9_]*)=(.*)$", re.S)
    found: list[str] = []
    for record in records:
        match = assignment.fullmatch(record)
        if not match:
            raise InvalidAssignment("ambiguous shell syntax; expected literal export assignments")
        record_name, rhs = match.groups()
        if record_name in ("AGENTSTACK_PROTECTED_ROOTS", "AGENTSTACK_EXTRA_PROTECTED_ROOTS"):
            value = literal_value(rhs)
            if record_name == name:
                found.append(value)
        else:
            # A separate export after a shell operator can set protection even
            # if the first assignment is to an unrelated key. Quoted operators
            # in unrelated values still belong to their own setting validator.
            lexer = shlex.shlex(record, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            lexer.commenters = "#"
            tokens = list(lexer)
            if any(token and set(token) <= set(";&|") for token in tokens):
                raise InvalidAssignment("multiple shell commands in installed settings")
            # A later export operand can assign protection even when the first
            # is unrelated. Support only the single operand the writer emits;
            # quoted unrelated values still belong to their own validators.
            if len(tokens) != 2:
                raise InvalidAssignment("multiple export operands; expected one assignment")
            if name in record and "\n" in record:
                raise InvalidAssignment("protection name in an ambiguous assignment or multiline literal")
    if len(found) > 1:
        raise InvalidAssignment("duplicate assignments")
    if not found:
        return "absent", ""
    return ("present-value" if found[0] else "present-empty"), found[0]


def main() -> int:
    mode, name, filename = sys.argv[1:]
    try:
        state, value = read_assignment(pathlib.Path(filename), name)
    except InvalidAssignment as error:
        if mode == "state":
            print("invalid")
        print(f"error: invalid {name} in {filename}: {error}; installed settings were not changed", file=sys.stderr)
        return 2
    if mode == "state":
        print(state)
    elif mode == "present":
        print("0" if state == "absent" else "1")
    elif mode == "--shell":
        if state != "absent":
            print("export " + name + "=" + shlex.quote(value), end="")
    elif mode == "value":
        print(value, end="")
    else:
        raise SystemExit(f"unknown mode: {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
