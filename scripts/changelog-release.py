#!/usr/bin/env python3
"""Collect changelog.d/*.md into a dated CHANGELOG section (stdlib only)."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RELEASE_TITLE = re.compile(r"\d{4}\.\d{2}\.\d{2}(?:\.\d+)?")
FRAGMENT_NAME = re.compile(r"\d{6}-[a-z0-9][a-z0-9-]*\.md")


def markdown_lines(text: str):
    """Yield source lines outside backtick/tilde fences, with character offsets."""
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
        elif marker and not (marker[1][0] == "`" and "`" in marker[2]):
            fence = (marker[1][0], len(marker[1]))
        else:
            yield offset, line
        offset += len(line)
    if fence:
        raise ValueError("unclosed fenced code block")


def markdown_headings(text: str):
    previous = None
    previous_end = 0
    for offset, line in markdown_lines(text):
        if offset != previous_end:
            previous = None
        previous_end = offset + len(line)
        match = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$", line.rstrip("\r\n"))
        if match:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", match[2] or "").strip()
            yield len(match[1]), title, offset
            previous = None
        elif previous and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", line.rstrip("\r\n")):
            yield (1 if line.lstrip().startswith("=") else 2), previous[1], previous[0]
            previous = None
        else:
            previous = (offset, line.strip()) if line.strip() else None


def validate_changelog(text: str) -> list[tuple[str, int]]:
    """One structural contract for CI and release; pending sections are forbidden."""
    releases = []
    for level, title, offset in markdown_headings(text):
        if level == 2:
            if not RELEASE_TITLE.fullmatch(title):
                raise ValueError("CHANGELOG: level 2 headings must be released dates; use changelog.d for pending items")
            datetime.strptime(title[:10], "%Y.%m.%d")
            releases.append((title, offset))
        elif level == 1:
            if title != "Changelog" or releases:
                raise ValueError("CHANGELOG: only the Changelog title may be level 1")
        elif not releases:
            raise ValueError("CHANGELOG: changes before first release; use changelog.d for pending items")
    return releases


def render_fragment(text: str) -> str:
    """Rebase ordinary links outside code fences from changelog.d to the root."""
    result = text
    for offset, line in reversed(list(markdown_lines(text))):
        updated = re.sub(r"(\]\()\.\./", r"\1", line)
        updated = re.sub(r"(^ {0,3}\[[^]\n]+\]:[ \t]*<?)\.\./", r"\1", updated)
        result = result[:offset] + updated + result[offset + len(line):]
    return result


def validate_fragment(path: Path) -> str:
    """Validate one whole fragment before CI accepts it or release consumes it."""
    if not FRAGMENT_NAME.fullmatch(path.name):
        raise ValueError(f"{path.name}: use NNNNNN-description.md (six-digit PR number)")
    text = path.read_text(encoding="utf-8")
    # Exhaust the shared scanner: it also rejects an unclosed fence at EOF.
    headings = list(markdown_headings(text))
    if not text.startswith("### ") or any(level < 3 for level, _, _ in headings):
        raise ValueError(f"{path.name}: start with ###; no level 1/2 headings")
    return text


def read_fragments(root: Path) -> list[tuple[Path, str]]:
    return [
        (path, validate_fragment(path))
        for path in sorted((root / "changelog.d").glob("*.md"), reverse=True)
    ]


def release_order(version: str) -> tuple[str, int]:
    """Compare validated release dates, then their numeric same-day revision."""
    return version[:10], int(version[11:] or "0")


def release(root: Path, version: str) -> int:
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}(?:\.[1-9]\d*)?", version):
        raise ValueError("version must be YYYY.MM.DD or YYYY.MM.DD.N (N >= 1)")
    datetime.strptime(version[:10], "%Y.%m.%d")
    changelog = root / "CHANGELOG.md"
    original = changelog.read_text(encoding="utf-8")
    headings = validate_changelog(original)
    fragments = read_fragments(root)
    if not fragments:
        return 0
    if any(title == version for title, _ in headings):
        raise ValueError("version already released; review remaining fragments")
    if headings and release_order(version) <= release_order(headings[0][0]):
        raise ValueError("version must be newer than the latest release")

    contents = [render_fragment(text) for _, text in fragments]
    # Keep each fragment's prose/spacing; add a separator only if needed.
    body = ""
    for text in contents:
        if body and not body.endswith("\n\n"):
            body += "\n" if body.endswith("\n") else "\n\n"
        body += text
    body = body.rstrip("\n") + "\n\n"
    position = headings[0][1] if headings else len(original)
    prefix = original[:position]
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n" if prefix.endswith("\n") else "\n\n"
    updated = prefix + f"## {version}\n\n" + body + original[position:]

    # Save notes atomically before deleting any source fragment. A failed
    # cleanup leaves a complete released section and never discards notes.
    descriptor, temporary = tempfile.mkstemp(prefix=".changelog-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, changelog.stat().st_mode & 0o777)
        os.replace(temporary, changelog)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    for path, _ in fragments:
        path.unlink()
    return len(fragments)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="new release: YYYY.MM.DD, or YYYY.MM.DD.N for a same-day revision")
    args = parser.parse_args()
    try:
        count = release(ROOT, args.version)
    except (OSError, ValueError) as error:
        parser.exit(1, f"changelog-release: {error}\n")
    print(f"Collected {count} changelog fragments.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
