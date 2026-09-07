"""Child sessions must not require zsh.

spawn_child.sh used to start every child tmux session with a hard-coded
`/bin/zsh -lc`, and the Codex launch snippet used zsh-only expansions
(`${(s.:.)VAR}`, `${=VAR}`). On a stock Ubuntu — WSL2 included — there is no
zsh, so the session died two seconds after spawn with an empty pane (seen on
WSL2 Ubuntu 26.04, 2026-09-07). The launcher now picks zsh when present and
bash otherwise, and the snippets are written in the subset both shells share.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPAWN = ROOT / "hooks" / "spawn_child.sh"


def _spawn_text() -> str:
    return SPAWN.read_text(encoding="utf-8")


def test_no_hard_coded_zsh_launch():
    text = _spawn_text()
    assert "/bin/zsh" not in text, "child launch must go through resolve_child_shell"
    assert text.count('"$CHILD_SHELL"\' -lc \'') == 4, (
        "both Codex and both Claude launch sites use the resolved shell"
    )


def test_launch_snippets_avoid_zsh_only_expansions():
    text = _spawn_text()
    assert not re.search(r"\$\{\(s", text), "${(s.:.)…} is zsh-only"
    assert not re.search(r"\$\{=", text), "${=…} is zsh-only"


def _codex_snippet() -> str:
    m = re.search(
        r'EXTRA_ARGS=\(\)\n(.*?)env -u OPENAI_API_KEY "\$AGENTSTACK_CODEX_BIN"',
        _spawn_text(),
        re.S,
    )
    assert m, "Codex launch snippet not found"
    return "EXTRA_ARGS=()\n" + m.group(1)


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_codex_snippet_splits_dirs_and_flags_the_same_in_both_shells(shell):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    with tempfile.TemporaryDirectory() as td:
        spaced = os.path.join(td, "dir with space")
        plain = os.path.join(td, "plain")
        os.makedirs(spaced)
        os.makedirs(plain)
        script = (
            _codex_snippet()
            + '\nprintf "%s\\n" "${EXTRA_ARGS[@]}"\n'
            + 'printf "[%s]\\n" $(printf "%s" "$AGENTSTACK_CODEX_APPROVAL")\n'
        )
        env = dict(
            os.environ,
            AGENTSTACK_CODEX_ADD_DIRS_RESOLVED=f"{spaced}:{plain}:{td}/missing",
            AGENTSTACK_CODEX_APPROVAL="--ask-for-approval never",
        )
        r = subprocess.run([shell, "-c", script], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines == [
        "--add-dir", spaced, "--add-dir", plain,
        "[--ask-for-approval]", "[never]",
    ], lines


def _resolve_child_shell(env: dict) -> subprocess.CompletedProcess:
    # Run only the function definition, not the launcher.
    text = _spawn_text()
    m = re.search(r"(resolve_child_shell\(\) \{.*?\n\})", text, re.S)
    assert m, "resolve_child_shell not found"
    return subprocess.run(
        ["bash", "-c", m.group(1) + "\nresolve_child_shell"],
        env=env, capture_output=True, text=True,
    )


def test_resolve_child_shell_falls_back_to_bash_without_zsh():
    with tempfile.TemporaryDirectory() as td:
        # A PATH with bash only: no zsh anywhere.
        fake_bin = pathlib.Path(td) / "bin"
        fake_bin.mkdir()
        os.symlink(shutil.which("bash"), fake_bin / "bash")
        r = _resolve_child_shell({"PATH": str(fake_bin), "HOME": td})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(fake_bin / "bash")


def test_resolve_child_shell_prefers_zsh_and_honours_override():
    with tempfile.TemporaryDirectory() as td:
        fake_bin = pathlib.Path(td) / "bin"
        fake_bin.mkdir()
        os.symlink(shutil.which("bash"), fake_bin / "bash")
        os.symlink(shutil.which("bash"), fake_bin / "zsh")
        r = _resolve_child_shell({"PATH": str(fake_bin), "HOME": td})
        assert r.stdout.strip() == str(fake_bin / "zsh"), r.stderr
        override = fake_bin / "mysh"
        os.symlink(shutil.which("bash"), override)
        r = _resolve_child_shell(
            {"PATH": str(fake_bin), "HOME": td, "AGENTSTACK_CHILD_SHELL": str(override)}
        )
        assert r.stdout.strip() == str(override), r.stderr


def test_dashboard_resume_uses_the_same_fallback(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "dash_server", ROOT / "dashboard" / "server.py"
    )
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)  # type: ignore[union-attr]
    monkeypatch.delenv("AGENTSTACK_CHILD_SHELL", raising=False)
    monkeypatch.setattr(server.shutil, "which", lambda name: None if name == "zsh" else "/usr/bin/bash")
    assert server._login_shell() == "/usr/bin/bash"
    monkeypatch.setattr(server.shutil, "which", lambda name: "/bin/zsh" if name == "zsh" else "/usr/bin/bash")
    assert server._login_shell() == "/bin/zsh"
