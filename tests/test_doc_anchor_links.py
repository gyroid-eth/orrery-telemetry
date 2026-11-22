"""Every local `docs/<file>.md#<anchor>` reference in shipped text must resolve.

Managed instruction blocks and CLI diagnostics send operators to docs anchors.
A renamed or missing heading silently turns that guidance into a dead link, so
check every reference against the GitHub-style slugs of the target's headings.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

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
        anchors.update(re.findall(r'<a\s+id=["\']([\w-]+)["\']\s*>', line))
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
        # Absolute web URLs have their own repository/document namespace.
        local_text = re.sub(r"https?://[^\s<>\"'`()]+", "", text, flags=re.IGNORECASE)
        for match in REFERENCE.finditer(local_text):
            doc = ROOT / "docs" / match.group(1)
            if not doc.is_file():
                continue
            checked += 1
            if match.group(2) not in _anchors(doc):
                rel = path.relative_to(ROOT)
                missing.append(f"{rel}: docs/{match.group(1)}#{match.group(2)}")
    assert checked > 0, "no docs anchor references found; the scanner is broken"
    assert not missing, "dead docs anchors:\n" + "\n".join(sorted(set(missing)))


def test_explicit_html_anchors_resolve_but_fenced_examples_do_not(tmp_path):
    doc=tmp_path/'guide.md'
    doc.write_text('# Visible heading\n<a id="stable-link"></a>\n```html\n<a id="example-only"></a>\n```\n')
    assert _anchors(doc)=={'visible-heading','stable-link'}


@pytest.mark.parametrize("scheme", ["https", "http"])
def test_external_docs_urls_do_not_resolve_against_local_docs(tmp_path, monkeypatch, scheme):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("# Local heading\n")
    source = tmp_path / "README.md"
    source.write_text(
        f"[External]({scheme}://example.org/other/docs/guide.md#foreign-heading)\n"
        "[Local](docs/guide.md#local-heading)\n"
        "[Relative](../docs/guide.md#local-heading)\n"
    )
    monkeypatch.setitem(test_docs_anchor_references_resolve.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(
        test_docs_anchor_references_resolve.__globals__, "_tracked_text_files", lambda: [source]
    )
    test_docs_anchor_references_resolve()


def test_external_url_does_not_hide_a_broken_local_reference(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("# Local heading\n")
    source = tmp_path / "README.md"
    source.write_text(
        "[External](https://example.org/other/docs/guide.md#foreign-heading)\n"
        "[Broken](docs/guide.md#missing-local-heading)\n"
    )
    monkeypatch.setitem(test_docs_anchor_references_resolve.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(
        test_docs_anchor_references_resolve.__globals__, "_tracked_text_files", lambda: [source]
    )
    with pytest.raises(AssertionError, match="missing-local-heading") as error:
        test_docs_anchor_references_resolve()
    assert "foreign-heading" not in str(error.value)
