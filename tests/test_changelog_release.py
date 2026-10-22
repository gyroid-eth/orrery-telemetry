"""Release-note assembly uses only temporary repository files."""

from __future__ import annotations

from pathlib import Path
import runpy
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/changelog-release.py"
release = runpy.run_path(str(SCRIPT))["release"]
OLD = "# Changelog\n\nIntroduction.\n\n---\n\n## 2026.10.06\n\n### Old\n\nKept.\n"


def repository(tmp_path: Path) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(OLD, encoding="utf-8")
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "VERSION").write_text("2026.10.06\n", encoding="utf-8")
    return tmp_path


def test_no_fragments_does_not_write(tmp_path):
    root = repository(tmp_path)
    before = (root / "CHANGELOG.md").stat().st_mtime_ns
    assert release(root, "2026.10.11") == 0
    assert (root / "CHANGELOG.md").read_text() == OLD
    assert (root / "CHANGELOG.md").stat().st_mtime_ns == before
    (root / "changelog.d").rmdir()
    assert release(root, "2026.10.11") == 0


def test_multiple_fragments_sorted_and_second_run_is_noop(tmp_path):
    root = repository(tmp_path)
    # Creation order deliberately differs from filename order.
    (root / "changelog.d/000251-last.md").write_text("### Last\n\nSecond.\n")
    (root / "changelog.d/000260-first.md").write_text("### First\n\nFirst.\n\n")
    (root / "changelog.d/keep.txt").write_text("Not a fragment.\n")
    assert release(root, "2026.10.11") == 2
    expected = OLD.replace(
        "## 2026.10.06",
        "## 2026.10.11\n\n### First\n\nFirst.\n\n### Last\n\nSecond.\n\n## 2026.10.06",
    )
    assert (root / "CHANGELOG.md").read_text() == expected
    assert not list((root / "changelog.d").glob("*.md"))
    assert (root / "changelog.d/keep.txt").exists()
    assert (root / "VERSION").read_text() == "2026.10.06\n"
    before = (root / "CHANGELOG.md").stat().st_mtime_ns
    assert release(root, "2026.10.11") == 0
    assert (root / "CHANGELOG.md").stat().st_mtime_ns == before


def test_initial_release_and_links(tmp_path):
    root = repository(tmp_path)
    (root / "CHANGELOG.md").write_text("# Changelog\n\nIntroduction.\n")
    (root / "changelog.d/000251-change.md").write_text(
        "### Links\n\n[API](../docs/api.md#version), [external](https://example.com).\n"
        "![Image](../docs/image.png) and [reference][api].\n\n"
        "[api]: ../docs/api.md\n"
    )
    assert release(root, "2026.10.11") == 1
    result = (root / "CHANGELOG.md").read_text()
    assert result.startswith("# Changelog\n\nIntroduction.\n\n## 2026.10.11\n\n")
    assert "[API](docs/api.md#version)" in result
    assert "[external](https://example.com)" in result
    assert "![Image](docs/image.png)" in result
    assert "[api]: docs/api.md" in result


@pytest.mark.parametrize("version", ["invalid", "2026.2.01", "2026.02.30", "2026.10.06", "2026.10.05",
                                     "2026.10.06.0", "2026.10.06.01", "2026.10.06.1.1", "2026.02.30.1"])
def test_invalid_or_reused_version_keeps_all_files(tmp_path, version):
    root = repository(tmp_path)
    fragment = root / "changelog.d/000251-new.md"
    fragment.write_text("### New\n\nPending.\n")
    with pytest.raises(ValueError):
        release(root, version)
    assert fragment.read_text() == "### New\n\nPending.\n"
    assert (root / "CHANGELOG.md").read_text() == OLD


@pytest.mark.parametrize("previous,version,allowed", [
    ("2026.10.06", "2026.10.06.1", True),
    ("2026.10.06.9", "2026.10.06.10", True),
    ("2026.10.06.10", "2026.10.06.9", False),
    ("2026.10.06.1", "2026.10.06", False),
    ("2026.10.06.1", "2026.10.06.1", False),
    ("2026.10.06.10", "2026.10.07", True),
])
def test_same_day_revision_preserves_released_notes(tmp_path, previous, version, allowed):
    root = repository(tmp_path)
    original = OLD.replace("## 2026.10.06", "## " + previous)
    (root / "CHANGELOG.md").write_text(original)
    fragment = root / "changelog.d/000350-new.md"
    fragment.write_text("### New\n\nFollow-up.\n")
    if allowed:
        assert release(root, version) == 1
        updated = (root / "CHANGELOG.md").read_text()
        assert "## " + version + "\n" in updated
        assert updated[updated.index("## " + previous + "\n"):] == original[original.index("## " + previous + "\n"):]
        assert not fragment.exists()
        assert release(root, version) == 0
    else:
        with pytest.raises(ValueError):
            release(root, version)
        assert (root / "CHANGELOG.md").read_text() == original
        assert fragment.read_text() == "### New\n\nFollow-up.\n"
    assert (root / "VERSION").read_text() == "2026.10.06\n"


@pytest.mark.parametrize("text", ["", "# Title\n", "Plain prose\n", "### Change\n\n## Unreleased\n"])
def test_malformed_fragment_keeps_all_files(tmp_path, text):
    root = repository(tmp_path)
    (root / "changelog.d/000252-valid.md").write_text("### Valid\n\nKept.\n")
    (root / "changelog.d/000251-bad.md").write_text(text)
    with pytest.raises(ValueError):
        release(root, "2026.10.11")
    assert (root / "CHANGELOG.md").read_text() == OLD
    assert len(list((root / "changelog.d").glob("*.md"))) == 2


def test_failed_changelog_write_keeps_fragments(tmp_path, monkeypatch):
    root = repository(tmp_path)
    (root / "changelog.d/000251-new.md").write_text("### New\n\nPending.\n")
    import os

    def fail_replace(*args):
        raise OSError("write failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="write failed"):
        release(root, "2026.10.11")
    assert (root / "CHANGELOG.md").read_text() == OLD
    assert (root / "changelog.d/000251-new.md").exists()
    assert not list(root.glob(".changelog-*"))


def test_cli_uses_script_checkout_not_current_directory(tmp_path):
    root = repository(tmp_path)
    (root / "scripts").mkdir()
    copied = root / "scripts/changelog-release.py"
    copied.write_bytes(SCRIPT.read_bytes())
    (root / "changelog.d/000251-new.md").write_text("### New\n\nPending.\n")
    result = subprocess.run(
        [sys.executable, str(copied), "2026.10.11"], cwd=root / "changelog.d",
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Collected 1" in result.stdout
    assert "## 2026.10.11" in (root / "CHANGELOG.md").read_text()


@pytest.mark.parametrize("heading", [
    "## Unreleased", "  ## Unreleased", "   ## [Unreleased]",
    "## uNrElEaSeD ##", "##\tUnreleased", "## 未リリース",
    "### Unreleased", "Unreleased\n----------",
])
@pytest.mark.parametrize("with_fragment", [False, True])
def test_pending_headings_rejected_by_ci_and_release(tmp_path, monkeypatch, heading, with_fragment):
    root = repository(tmp_path)
    text = OLD.replace("## 2026.10.06", heading + "\n\nPending.\n\n## 2026.10.06")
    (root / "CHANGELOG.md").write_text(text)
    if with_fragment:
        (root / "changelog.d/000251-new.md").write_text("### New\n\nPending.\n")
    # Call the real CI assertion with the same invalid source as release.
    guard = runpy.run_path(str(ROOT / "tests/test_docs_consistency.py"))[
        "test_pending_changelog_items_use_fragments"
    ]
    monkeypatch.setitem(guard.__globals__, "_read", lambda _: text)
    with pytest.raises(AssertionError, match="use changelog.d"):
        guard()
    with pytest.raises(ValueError, match="use changelog.d"):
        release(root, "2026.10.11")
    assert (root / "CHANGELOG.md").read_text() == text
    assert len(list((root / "changelog.d").glob("*.md"))) == int(with_fragment)


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_fenced_examples_are_preserved_not_checked_as_headings(tmp_path, fence):
    root = repository(tmp_path)
    snippet = f"{fence}bash\n# Comment\n## [Unreleased]\n[example](../docs/api.md)\n{fence}\n"
    # The same scanner protects both existing history and pending fragments.
    (root / "CHANGELOG.md").write_text(OLD + "\n" + snippet)
    (root / "changelog.d/000251-new.md").write_text("### Example\n\n" + snippet)
    assert release(root, "2026.10.11") == 1
    assert (root / "CHANGELOG.md").read_text().count(snippet) == 2


def test_migrated_and_new_fragments_use_descending_pr_numbers(tmp_path):
    root = repository(tmp_path)
    for name in (
        "000248-instruction-migration", "000260-branch-name",
        "000249-changelog-fragments", "000251-new-feature",
    ):
        (root / f"changelog.d/{name}.md").write_text(f"### {name}\n\nChange.\n")
    assert release(root, "2026.10.11") == 4
    result = (root / "CHANGELOG.md").read_text()
    headings = [line for line in result.splitlines() if line.startswith("### 000")]
    assert headings == [
        "### 000260-branch-name", "### 000251-new-feature",
        "### 000249-changelog-fragments", "### 000248-instruction-migration",
    ]


@pytest.mark.parametrize("name", ["251-old.md", "branch-name.md", "README.md"])
def test_other_md_names_are_rejected_not_consumed(tmp_path, name):
    root = repository(tmp_path)
    (root / "changelog.d" / name).write_text("### New\n\nPending.\n")
    with pytest.raises(ValueError, match="six-digit PR number"):
        release(root, "2026.10.11")
    assert (root / "CHANGELOG.md").read_text() == OLD
    assert (root / "changelog.d" / name).exists()


def test_released_checkout_keeps_fragment_directory_and_links(tmp_path):
    root = repository(tmp_path)
    (root / "CHANGELOG.md").write_text(OLD.replace("Introduction.", "[changelog.d/](changelog.d/)"))
    (root / "changelog.d/README.txt").write_bytes((ROOT / "changelog.d/README.txt").read_bytes())
    (root / "changelog.d/000251-new.md").write_text("### New\n\nPending.\n")
    assert release(root, "2026.10.11") == 1
    # Git stores files, not empty directories. Recreate exactly the release index.
    subprocess.run(["git", "-c", "init.templateDir=", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    fresh = root / "fresh"
    subprocess.run([
        "git", "-C", str(root), "checkout-index", "--all", f"--prefix={fresh}/",
    ], check=True)
    assert (fresh / "changelog.d/README.txt").is_file()
    assert not list((fresh / "changelog.d").glob("*.md"))
    assert "[changelog.d/](changelog.d/)" in (fresh / "CHANGELOG.md").read_text()
    assert release(fresh, "2026.10.11") == 0


def fragment_ci_check(root, monkeypatch):
    (root / "scripts").mkdir()
    (root / "scripts/changelog-release.py").write_bytes(SCRIPT.read_bytes())
    guard = runpy.run_path(str(ROOT / "tests/test_docs_consistency.py"))[
        "test_changelog_fragments_are_valid"
    ]
    monkeypatch.setitem(guard.__globals__, "ROOT", root)
    return guard


@pytest.mark.parametrize("name,text", [
    ("251-old.md", "### New\n\nPending.\n"),
    ("000251-new.md", "Pending without a heading.\n"),
    ("000251-new.md", "### New\n\n# Invalid heading\n"),
    ("000251-new.md", "### New\n\n## Invalid heading\n"),
    ("000251-new.md", "### New\n\n```text\nUnclosed.\n"),
    ("000251-new.md", "### New\n\n~~~text\nUnclosed.\n"),
    ("000251-new.md", "### New\n\n````text\nToo short.\n```\n"),
    ("000251-new.md", "### New\n\n```text\nWrong marker.\n~~~\n"),
    ("000251-new.md", "### New\n\n```text\nNot a closing fence.\n``` trailing\n"),
])
def test_ci_and_release_reject_invalid_fragments_before_mutation(tmp_path, monkeypatch, name, text):
    root = repository(tmp_path)
    (root / "changelog.d" / name).write_text(text)
    # A malformed fragment must not swallow the next fragment or older releases.
    following = root / "changelog.d/000250-other.md"
    following.write_text("### Other PR\n\nPreserved.\n")
    before = {p.name: p.read_bytes() for p in (root / "changelog.d").iterdir()}
    guard = fragment_ci_check(root, monkeypatch)
    with pytest.raises(ValueError):
        guard()
    with pytest.raises(ValueError):
        release(root, "2026.10.11")
    assert (root / "CHANGELOG.md").read_text() == OLD
    assert before == {p.name: p.read_bytes() for p in (root / "changelog.d").iterdir()}
    assert not list(root.glob(".changelog-*"))


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_ci_accepts_closed_fences_and_release_preserves_history(tmp_path, monkeypatch, fence):
    root = repository(tmp_path)
    text = f"### Example\n\n{fence}bash\n# Comment\n## [Unreleased]\n{fence}\n"
    (root / "changelog.d/000251-example.md").write_text(text)
    guard = fragment_ci_check(root, monkeypatch)
    guard()
    assert release(root, "2026.10.11") == 1
    result = (root / "CHANGELOG.md").read_text()
    assert text in result
    assert result.endswith(OLD[OLD.index("## 2026.10.06"):])


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_unclosed_fences_in_separate_fragments_do_not_close_each_other(tmp_path, fence):
    root = repository(tmp_path)
    for number in (250, 251):
        (root / f"changelog.d/{number:06d}-example.md").write_text(f"### Example\n\n{fence}\n")
    with pytest.raises(ValueError, match="unclosed fenced code block"):
        release(root, "2026.10.11")
    assert (root / "CHANGELOG.md").read_text() == OLD
    assert len(list((root / "changelog.d").glob("*.md"))) == 2
