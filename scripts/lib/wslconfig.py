#!/usr/bin/env python3
"""Read the WSL idle settings from a Windows `.wslconfig`, read-only.

ORRERY no longer writes this file: the dashboard keeps the distro running
while agents work (dashboard/wsl_anchor.py). An earlier install, or the user,
may have set

  [general] instanceIdleTimeout=-1   every distro runs until `wsl --shutdown`
  [wsl2]    vmIdleTimeout=-1         the WSL VM (and its memory) never idles out

and doctor explains them. Only a small, explicit subset of the syntax is
understood, and anything outside it is reported as `unknown` rather than
guessed: a value is an integer in the int32 range with an optional `#`
comment; a section header is `[name]` with no spaces inside; the same key
twice in a section is ambiguous. Names are matched exactly as WSL documents
them (`general`, `instanceIdleTimeout`, ...).

  wslconfig.py path
      Prints the Linux path of the Windows %USERPROFILE%\.wslconfig (exit 1
      when Windows interop cannot tell).
  wslconfig.py read PATH
      Prints JSON: {"instanceIdleTimeout": {"state": S, "value": V},
                    "vmIdleTimeout": {...}} where S is `unset`, `set`
      (V is the integer) or `unknown` (V is the reason).
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

KEYS = {("general", "instanceIdleTimeout"), ("wsl2", "vmIdleTimeout")}
INT32 = (-(2**31), 2**31 - 1)

_SECTION = re.compile(r"^\[([A-Za-z][A-Za-z0-9._-]*)\]\s*(?:#.*)?$")
_ENTRY = re.compile(r"^([A-Za-z][A-Za-z0-9._]*)\s*=\s*(.*?)\s*$")
_INTEGER = re.compile(r"^-?[0-9]+$")


def _decode(data: bytes) -> str | None:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def parse(text: str) -> dict:
    seen: dict[str, list[tuple[int, str]]] = {key: [] for _, key in KEYS}
    problems: dict[str, str] = {}
    bad_header = None
    section = None
    lowered = {(s.lower(), k.lower()): k for s, k in KEYS}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            match = _SECTION.match(line)
            section = match.group(1) if match else None
            if not match and bad_header is None:
                bad_header = f"line {number}: unrecognised section header {line!r}"
            continue
        match = _ENTRY.match(line)
        if not match or section is None:
            continue
        key = match.group(1)
        if (section, key) in KEYS:
            seen[key].append((number, match.group(2)))
        elif (section.lower(), key.lower()) in lowered:
            name = lowered[(section.lower(), key.lower())]
            problems.setdefault(name, f"line {number}: written as [{section}] {key}")
    found: dict[str, dict] = {}
    for _, key in KEYS:
        entries = seen[key]
        if len(entries) > 1:
            found[key] = {"state": "unknown", "value": f"{key} appears {len(entries)} times"}
        elif key in problems:
            found[key] = {"state": "unknown", "value": problems[key]}
        elif entries:
            number, raw = entries[0]
            value = raw.split("#", 1)[0].strip()
            if _INTEGER.match(value) and INT32[0] <= int(value) <= INT32[1]:
                found[key] = {"state": "set", "value": int(value)}
            else:
                found[key] = {"state": "unknown", "value": f"line {number}: {key}={raw} is not a plain integer"}
        elif bad_header:
            found[key] = {"state": "unknown", "value": bad_header}
        else:
            found[key] = {"state": "unset", "value": None}
    return found


def read(path: pathlib.Path) -> dict:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return parse("")
    text = _decode(data)
    if text is None:
        return {key: {"state": "unknown", "value": "not UTF-8 text"} for _, key in KEYS}
    return parse(text)


def windows_profile(cmd: str = "cmd.exe", wslpath: str = "wslpath") -> pathlib.Path | None:
    """%USERPROFILE% as a Linux path, or None.

    `set USERPROFILE` prints the value without re-reading it as a command
    (a profile path with `&` in it stays one value), `/u` makes cmd write
    UTF-16LE whatever the console code page is (a Japanese user name comes
    through intact), and `/d` skips AutoRun commands.
    """
    try:
        out = subprocess.run([cmd, "/d", "/u", "/c", "set USERPROFILE"], cwd="/",
                             capture_output=True, timeout=15).stdout
        text = out.decode("utf-16-le")
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    value = next((line[len("USERPROFILE="):] for line in text.splitlines()
                  if line.upper().startswith("USERPROFILE=")), "").strip()
    if not value:
        return None
    try:
        result = subprocess.run([wslpath, "-u", value], capture_output=True, timeout=15)
        linux = result.stdout.decode("utf-8").strip()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    if result.returncode != 0 or not linux or not pathlib.Path(linux).is_dir():
        return None
    return pathlib.Path(linux)


def main(argv: list[str]) -> int:
    if argv == ["path"]:
        profile = windows_profile()
        if profile is None:
            return 1
        print(profile / ".wslconfig")
        return 0
    if len(argv) != 2 or argv[0] != "read":
        print("usage: wslconfig.py path | read PATH", file=sys.stderr)
        return 2
    try:
        print(json.dumps(read(pathlib.Path(argv[1])), sort_keys=True))
    except OSError as exc:
        print(f"{argv[1]}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
