"""doctor / install: a managed block is ok only when it matches the template.

The begin marker alone used to count as ok. On WSL (2026-09-29) that let a
block from before the proxy routes (the "Always try the token-safe helper
first" version, tests/fixtures/managed_blocks/codex-AGENTS-helper-first.md,
which is codex/AGENTS.md at 864ba62^) keep Codex parents re-registering while
doctor reported the block healthy.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

from service_teardown import TEST_LABEL_PREFIX


ROOT = pathlib.Path(__file__).resolve().parents[1]
OLD_CODEX_TEMPLATE = ROOT / "tests" / "fixtures" / "managed_blocks" / "codex-AGENTS-helper-first.md"
CODEX_BEGIN = "<!-- >>> claude-agent-stack (managed: agentstack-codex-setup) -->"
CLAUDE_BEGIN = "<!-- >>> claude-agent-stack (managed: agentstack-claude-setup) -->"
END = "<!-- <<< claude-agent-stack -->"
PROJECT = "/work/project-a"


@pytest.fixture
def install(tmp_path):
    """A minimal installed layout: the setup scripts, their lib and templates."""
    home = tmp_path / "agentstack"
    (home / "bin" / "lib").mkdir(parents=True)
    for name in ("agentstack-codex-setup", "agentstack-claude-setup"):
        shutil.copy2(ROOT / "bin" / name, home / "bin" / name)
    shutil.copy2(ROOT / "bin" / "lib" / "agentstack-managed-block.sh",
                 home / "bin" / "lib" / "agentstack-managed-block.sh")
    for rel in ("codex/AGENTS.md", "claude/CLAUDE.md"):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, home / rel)
    return home


def _env(tmp_path, home, **extra):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "userhome"),
        "AGENTSTACK_HOME": str(home),
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
        "CODEX_HOME": str(tmp_path / "codex"),
        "AGENTSTACK_PROJECT_KEY": PROJECT,
    }
    env.update(extra)
    return env


def _run(home, script, env, *args):
    return subprocess.run(
        ["/bin/bash", str(home / "bin" / script), *args],
        env=env, text=True, capture_output=True, check=False,
    )


def _render(template: pathlib.Path, home: pathlib.Path, begin: str, project=PROJECT) -> str:
    body = template.read_text(encoding="utf-8")
    body = body.replace("__AGENTSTACK_HOME__", str(home)).replace(
        "__AGENTSTACK_PROJECT_KEY__", project)
    return f"{begin}\n{body}{END}\n"


def _write_codex(tmp_path, text):
    target = tmp_path / "codex" / "AGENTS.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# --check states
# --------------------------------------------------------------------------- #
def test_setup_output_is_current(tmp_path, install):
    env = _env(tmp_path, install)
    assert _run(install, "agentstack-codex-setup", env).returncode == 0
    result = _run(install, "agentstack-codex-setup", env, "--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("ok: Codex AGENTS.md managed block in ")
    assert "matches the installed template" in result.stdout


def test_the_old_helper_first_block_is_reported_with_the_update_command(tmp_path, install):
    _write_codex(tmp_path, "# my notes\n\n" + _render(OLD_CODEX_TEMPLATE, install, CODEX_BEGIN))
    result = _run(install, "agentstack-codex-setup", _env(tmp_path, install), "--check")
    assert result.returncode == 1
    out = result.stdout
    assert out.startswith("warn: Codex AGENTS.md managed block in ")
    assert "differs from the installed template" in out
    assert f"AGENTSTACK_HOME={install} " in out
    assert f"CODEX_HOME={tmp_path / 'codex'}" in out
    assert f"AGENTSTACK_PROJECT_KEY={PROJECT}" in out
    assert f"{install}/bin/agentstack-codex-setup" in out


def test_text_outside_the_block_does_not_make_it_stale(tmp_path, install):
    env = _env(tmp_path, install)
    _run(install, "agentstack-codex-setup", env)
    target = tmp_path / "codex" / "AGENTS.md"
    target.write_text("# personal rules\nuse tabs\n\n" + target.read_text() + "\n# more notes\n")
    assert _run(install, "agentstack-codex-setup", env, "--check").returncode == 0


def test_a_block_for_another_project_differs(tmp_path, install):
    _write_codex(tmp_path, _render(install / "codex" / "AGENTS.md", install, CODEX_BEGIN,
                                   project="/work/other-project"))
    result = _run(install, "agentstack-codex-setup", _env(tmp_path, install), "--check")
    assert result.returncode == 1
    assert "differs from the installed template" in result.stdout


def test_missing_file_and_missing_block(tmp_path, install):
    env = _env(tmp_path, install)
    result = _run(install, "agentstack-codex-setup", env, "--check")
    assert result.returncode == 1
    assert "managed block not found in" in result.stdout and "add it with:" in result.stdout
    _write_codex(tmp_path, "# only personal rules\n")
    result = _run(install, "agentstack-codex-setup", env, "--check")
    assert result.returncode == 1
    assert "managed block not found in" in result.stdout


@pytest.mark.parametrize("text, why", [
    (f"{CODEX_BEGIN}\nbody without an end\n", "no end marker after the begin marker"),
    (f"{CODEX_BEGIN}\nx\n{END}\n{CODEX_BEGIN}\ny\n{END}\n", "more than one begin marker"),
    (f"{CODEX_BEGIN}\nx\n{CODEX_BEGIN}\ny\n{END}\n", "more than one begin marker"),
])
def test_broken_markers_are_malformed_not_ok(tmp_path, install, text, why):
    _write_codex(tmp_path, text)
    result = _run(install, "agentstack-codex-setup", _env(tmp_path, install), "--check")
    assert result.returncode == 1
    assert f"is malformed ({why})" in result.stdout


def test_an_end_marker_of_another_block_before_ours_is_ignored(tmp_path, install):
    env = _env(tmp_path, install)
    _run(install, "agentstack-codex-setup", env)
    target = tmp_path / "codex" / "AGENTS.md"
    target.write_text(f"{END}\nstray\n\n" + target.read_text())
    assert _run(install, "agentstack-codex-setup", env, "--check").returncode == 0


def test_a_missing_template_is_not_reported_as_compared(tmp_path, install):
    env = _env(tmp_path, install)
    _run(install, "agentstack-codex-setup", env)
    (install / "codex" / "AGENTS.md").unlink()
    result = _run(install, "agentstack-codex-setup", env, "--check")
    assert result.returncode == 1
    assert result.stdout.startswith("warn: cannot check Codex AGENTS.md managed block")
    assert "template not found" in result.stdout
    assert "restore the install with scripts/install.sh" in result.stdout
    assert f"AGENTSTACK_HOME={install} " in result.stdout


def test_check_writes_nothing(tmp_path, install):
    old = _render(OLD_CODEX_TEMPLATE, install, CODEX_BEGIN)
    target = _write_codex(tmp_path, old)
    _run(install, "agentstack-codex-setup", _env(tmp_path, install), "--check")
    assert target.read_text() == old
    assert list(target.parent.iterdir()) == [target]  # no backup either


def test_claude_check_per_scope(tmp_path, install):
    project = tmp_path / "project"
    project.mkdir()
    env = _env(tmp_path, install, AGENTSTACK_PROJECT_KEY=str(project),
               AGENTSTACK_CLAUDE_MD_SCOPE="project")
    assert _run(install, "agentstack-claude-setup", env).returncode == 0
    ok = _run(install, "agentstack-claude-setup", env, "--check")
    assert ok.returncode == 0 and "ok: Claude CLAUDE.md managed block in" in ok.stdout
    claude_md = project / "CLAUDE.md"
    claude_md.write_text(claude_md.read_text().replace("Delegation", "Delegating", 1))
    stale = _run(install, "agentstack-claude-setup", env, "--check")
    assert stale.returncode == 1
    assert "differs from the installed template" in stale.stdout
    assert "AGENTSTACK_CLAUDE_MD_SCOPE=project" in stale.stdout
    assert f"AGENTSTACK_HOME={install} " in stale.stdout


def test_claude_check_without_a_project_key_says_it_cannot_check(tmp_path, install):
    env = _env(tmp_path, install, AGENTSTACK_PROJECT_KEY="", AGENTSTACK_CLAUDE_MD_SCOPE="project")
    result = _run(install, "agentstack-claude-setup", env, "--check")
    assert result.returncode == 1
    assert result.stdout.startswith("warn: cannot check Claude CLAUDE.md managed block:")
    assert "AGENTSTACK_PROJECT_KEY is required" in result.stdout
    assert "export AGENTSTACK_PROJECT_KEY=<project path> (or AGENTSTACK_CLAUDE_MD_SCOPE=global)" in result.stdout
    retry = result.stdout.split("then run: ", 1)[1].strip()
    assert "AGENTSTACK_PROJECT_KEY" not in retry and "AGENTSTACK_CLAUDE_MD_SCOPE" not in retry
    # The suggested command honours what the user sets next.
    project = tmp_path / "later-project"
    project.mkdir()
    for extra in ({"AGENTSTACK_PROJECT_KEY": str(project)},
                  {"AGENTSTACK_CLAUDE_MD_SCOPE": "global", "CLAUDE_HOME": str(tmp_path / "claude")}):
        ran = subprocess.run(["/bin/bash", "-c", retry], env={**env, **extra},
                             text=True, capture_output=True, check=False)
        assert "cannot check" not in ran.stdout, (extra, ran.stdout)
        assert "managed block not found in" in ran.stdout, (extra, ran.stdout)


# --------------------------------------------------------------------------- #
# doctor and install use the same check
# --------------------------------------------------------------------------- #
def _doctor_function(name: str) -> str:
    text = (ROOT / "scripts" / "doctor.sh").read_text(encoding="utf-8")
    start = text.index(f"\n{name}() {{") + 1
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def test_doctor_reports_the_old_block_instead_of_ok(tmp_path, install):
    _write_codex(tmp_path, _render(OLD_CODEX_TEMPLATE, install, CODEX_BEGIN))
    script = "\n".join((
        f"INSTALL_DIR={str(install)!r}",
        _doctor_function("check_managed_block"),
        f'check_managed_block "Codex AGENTS.md" agentstack-codex-setup CODEX_HOME={str(tmp_path / "codex")!r}',
        'echo "doctor continued"',
    ))
    result = subprocess.run(["/bin/bash", "-c", script], env=_env(tmp_path, install),
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0
    assert "warn: Codex AGENTS.md managed block" in result.stdout
    assert "differs from the installed template" in result.stdout
    assert "ok: Codex AGENTS.md" not in result.stdout
    assert "doctor continued" in result.stdout  # a stale block is a warning, not a stop


def test_doctor_without_the_setup_script_says_so(tmp_path, install):
    (install / "bin" / "agentstack-codex-setup").unlink()
    script = "\n".join((
        f"INSTALL_DIR={str(install)!r}",
        _doctor_function("check_managed_block"),
        'check_managed_block "Codex AGENTS.md" agentstack-codex-setup CODEX_HOME=/nowhere',
    ))
    result = subprocess.run(["/bin/bash", "-c", script], env=_env(tmp_path, install),
                            text=True, capture_output=True, check=False)
    assert "warn: cannot check Codex AGENTS.md managed block" in result.stdout


def test_doctor_and_install_no_longer_trust_the_begin_marker_alone():
    doctor = (ROOT / "scripts" / "doctor.sh").read_text(encoding="utf-8")
    assert "warn_managed_block" not in doctor
    assert doctor.count("check_managed_block ") >= 2  # Codex and Claude
    install = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "report_managed_instructions" in install
    assert '--check' in install


@pytest.mark.parametrize("script, env_extra", [
    ("agentstack-codex-setup", {}),
    ("agentstack-claude-setup", {"AGENTSTACK_CLAUDE_MD_SCOPE": "global"}),
])
def test_the_printed_update_command_updates_the_checked_install(tmp_path, install, script, env_extra):
    """Run the command --check prints, from a shell whose AGENTSTACK_HOME
    points elsewhere (the default install): the block must come from the
    checked install's template, and --check must then report it current."""
    env = _env(tmp_path, install, CLAUDE_HOME=str(tmp_path / "claude"), **env_extra)
    first = _run(install, script, env, "--check")
    assert first.returncode == 1
    command = first.stdout.split("with: ", 1)[1].strip()
    other_home = tmp_path / "default-install"
    (other_home / "codex").mkdir(parents=True)
    (other_home / "claude").mkdir(parents=True)
    (other_home / "codex" / "AGENTS.md").write_text("WRONG TEMPLATE\n")
    (other_home / "claude" / "CLAUDE.md").write_text("WRONG TEMPLATE\n")
    shell_env = {**env, "AGENTSTACK_HOME": str(other_home),
                 "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX}
    ran = subprocess.run(["/bin/bash", "-c", command], env=shell_env,
                         text=True, capture_output=True, check=False)
    assert ran.returncode == 0, ran.stderr
    again = _run(install, script, env, "--check")
    assert again.returncode == 0, again.stdout
    assert "WRONG TEMPLATE" not in (tmp_path / ("codex/AGENTS.md" if "codex" in script else "claude/CLAUDE.md")).read_text()
