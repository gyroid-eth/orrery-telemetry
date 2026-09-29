#!/usr/bin/env python3
"""Read and set `[general] instanceIdleTimeout` in a Windows `.wslconfig`.

WSL stops a distro 15 seconds after its last Windows-side client (wsl.exe,
a Windows Terminal tab) exits, whatever is still running inside it: tmux, the
dashboard and every agent go with it. `[wsl2] vmIdleTimeout` keeps only the
VM; linger and systemd keep nothing. `instanceIdleTimeout=-1` is what keeps
the distro (checked on WSL 2.7.13, 2026-09-29: without it the distro stopped
13-16 s after the last client closed; with it, it stayed running).

  wslconfig.py read PATH
      Prints `unset`, `ok <value>` (negative: never stop) or
      `other <value>` (any other value, stops after that many ms).
  wslconfig.py ensure PATH --backup-dir DIR [--dry-run]
      Adds `instanceIdleTimeout=-1` under `[general]` unless the key is
      already there. Prints `created`, `added <backup>` or `present <value>`
      (the value is never changed). With --dry-run prints `would-create`,
      `would-add` or `present <value>` and writes nothing.

Everything else in the file is kept as it was. The file is written as UTF-8
without a BOM with CRLF line endings, the form Windows editors produce. A file
that is not UTF-8 is left alone (exit 3), since rewriting it could lose text.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import pathlib
import re
import sys
import tempfile

SECTION = "general"
KEY = "instanceIdleTimeout"
VALUE = "-1"

_SECTION_RE = re.compile(r"^\s*\[\s*([^\]]*?)\s*\]\s*(?:[#;].*)?$")
_KEY_RE = re.compile(r"^\s*([^=#;\s][^=]*?)\s*=\s*(.*?)\s*$")


class NotUtf8(Exception):
    pass


def _read_lines(path: pathlib.Path) -> list[str] | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotUtf8(str(exc)) from exc
    return text.splitlines()


def _find(lines: list[str]) -> tuple[int | None, int | None, str | None]:
    """Return (index of the [general] header, insertion index, existing value).

    Section and key names are compared case-insensitively, so a key someone
    wrote as `instanceidletimeout` counts as present and is left alone.
    """
    header = None
    insert = None
    current = None
    in_first = False
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match:
            current = match.group(1).lower()
            in_first = current == SECTION and header is None
            if in_first:
                header = index
                insert = index + 1
            continue
        if current != SECTION:
            continue
        match = _KEY_RE.match(line)
        if match and match.group(1).lower() == KEY.lower():
            value = re.split(r"\s+[#;]", match.group(2), maxsplit=1)[0].strip()
            return header, insert, value
        if in_first and line.strip():
            # Right after the section's last non-blank line, so the blank
            # lines before the next section stay where they were.
            insert = index + 1
    return header, insert, None


def classify(value: str | None) -> str:
    if value is None:
        return "unset"
    try:
        number = int(value, 0)
    except ValueError:
        return f"other {value}"
    return f"ok {value}" if number < 0 else f"other {value}"


def _write(path: pathlib.Path, lines: list[str]) -> None:
    payload = ("\r\n".join(lines) + "\r\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".wslconfig.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _backup(path: pathlib.Path, backup_root: pathlib.Path) -> pathlib.Path:
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_dir = backup_root / stamp
    counter = 1
    while (backup_dir / "wslconfig").exists():
        backup_dir = backup_root / f"{stamp}.{counter}"
        counter += 1
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / "wslconfig"
    target.write_bytes(path.read_bytes())
    return target


def ensure(path: pathlib.Path, backup_root: pathlib.Path, dry_run: bool) -> str:
    lines = _read_lines(path)
    if lines is None:
        if dry_run:
            return "would-create"
        _write(path, [f"[{SECTION}]", f"{KEY}={VALUE}"])
        return "created"
    header, insert, value = _find(lines)
    if value is not None:
        return f"present {value}"
    if dry_run:
        return "would-add"
    backup = _backup(path, backup_root)
    if header is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines += [f"[{SECTION}]", f"{KEY}={VALUE}"]
    else:
        lines.insert(insert, f"{KEY}={VALUE}")
    _write(path, lines)
    return f"added {backup}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    read = sub.add_parser("read")
    read.add_argument("path")
    put = sub.add_parser("ensure")
    put.add_argument("path")
    put.add_argument("--backup-dir", required=True)
    put.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    path = pathlib.Path(args.path)
    try:
        if args.command == "read":
            lines = _read_lines(path)
            print(classify(None if lines is None else _find(lines)[2]))
        else:
            print(ensure(path, pathlib.Path(args.backup_dir), args.dry_run))
    except NotUtf8 as exc:
        print(f"{path} is not UTF-8 text ({exc}); left unchanged", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
