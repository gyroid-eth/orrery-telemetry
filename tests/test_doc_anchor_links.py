"""Every `docs/<file>.md#<anchor>` reference in shipped text must resolve.

Managed instruction blocks and CLI diagnostics send operators to docs anchors.
A renamed or missing heading silently turns that guidance into a dead link, so
check every reference against the GitHub-style slugs of the target's headings.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = re.compile(r"docs/([A-Za-z0-9_.-]+\.md)#([\w-]+)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


def _slug(heading: str) -> str:
    text = re.sub(r"`", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(doc: Path) -> set[str]:
    anchors: set[str] = set()
    in_fence = False
    for line in doc.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING.match(line)
        if match:
            anchors.add(_slug(match.group(1)))
    return anchors


def _tracked_text_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    return [ROOT / name for name in listed if not name.startswith("tests/")]


def test_docs_anchor_references_resolve() -> None:
    missing: list[str] = []
    checked = 0
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for match in REFERENCE.finditer(text):
            doc = ROOT / "docs" / match.group(1)
            if not doc.is_file():
                continue
            checked += 1
            if match.group(2) not in _anchors(doc):
                rel = path.relative_to(ROOT)
                missing.append(f"{rel}: docs/{match.group(1)}#{match.group(2)}")
    assert checked > 0, "no docs anchor references found; the scanner is broken"
    assert not missing, "dead docs anchors:\n" + "\n".join(sorted(set(missing)))
