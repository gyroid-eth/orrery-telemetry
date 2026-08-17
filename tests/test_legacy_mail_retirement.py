"""Installing must not leave the predecessor mail server running.

Reported by a tester on 2026-08-17: after the cutover both jobs stayed loaded
and both kept listening. Two mechanisms could have retired the old one and
neither matched what was actually installed -- the same-port handoff never fires
because the new server binds a different port, and the legacy label it looks for
is a single guessed string (``com.<user>.mcp-agent-mail``) while older
installers registered ``org.agentstack.mcp-agent-mail``.

The two servers own separate databases, so agent identities and file
reservations split across two stores depending on which endpoint a client
reaches. That is the harm; the wasted process is incidental.

These tests drive the installer's retirement step with a fake ``launchctl`` on
PATH, so nothing here touches a real service.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install.sh"

FAKE_LAUNCHCTL = """#!/bin/bash
# Records every call, and reports a job as loaded only while its marker exists.
echo "$@" >> "$LAUNCHCTL_LOG"
case "$1" in
  print)
    label="${2##*/}"
    [[ -f "$LOADED_DIR/$label" ]] && exit 0
    exit 113
    ;;
  bootout)
    label="${2##*/}"
    rm -f "$LOADED_DIR/$label"
    exit 0
    ;;
esac
exit 0
"""


def _harness(tmp_path: Path, loaded: list[str]) -> tuple[dict[str, str], Path, Path]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    launchctl = fakebin / "launchctl"
    launchctl.write_text(FAKE_LAUNCHCTL, encoding="utf-8")
    launchctl.chmod(0o755)

    loaded_dir = tmp_path / "loaded"
    loaded_dir.mkdir()
    agents = tmp_path / "home" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for label in loaded:
        (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
        (agents / f"{label}.plist").write_text("<plist/>\n", encoding="utf-8")

    log = tmp_path / "launchctl.log"
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path / "home"),
        "LAUNCHCTL_LOG": str(log),
        "LOADED_DIR": str(loaded_dir),
    }
    return env, loaded_dir, log


def _run_retirement(
    tmp_path: Path,
    loaded: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    keep_legacy: bool = False,
) -> tuple[str, Path, Path]:
    """Source the installer and call the step directly, with no install running."""
    env, loaded_dir, log = _harness(tmp_path, loaded)
    env.update(extra_env or {})
    install_dir = tmp_path / "agentstack"
    install_dir.mkdir()
    script = f"""
set -euo pipefail
# The installer runs its own argument parsing and main() at the bottom; source
# only the definitions by stopping before it acts.
INSTALL_SH={INSTALLER!s}
eval "$(sed -n '/^retire_legacy_mail_services() {{/,/^}}/p' "$INSTALL_SH")"
say() {{ echo "$@"; }}
warn() {{ echo "warn: $@"; }}
DRY_RUN={'true' if False else 'false'}
KEEP_LEGACY_MAIL={'true' if keep_legacy else 'false'}
INSTALL_DIR={install_dir!s}
uname() {{ echo Darwin; }}
retire_legacy_mail_services
"""
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout, loaded_dir, log


def test_the_label_older_installers_actually_used_is_retired(tmp_path: Path) -> None:
    label = "org.agentstack.mcp-agent-mail"
    out, loaded_dir, log = _run_retirement(tmp_path, [label])
    assert not (loaded_dir / label).exists(), "the legacy job was left loaded"
    assert f"bootout gui/{os.getuid()}/{label}" in log.read_text()
    assert "retired legacy mail service" in out
    parked = tmp_path / "agentstack" / "parked-launchd" / f"{label}.plist"
    assert parked.exists(), "the plist was deleted instead of parked"


def test_the_per_user_label_is_retired_too(tmp_path: Path) -> None:
    # The installer builds this label from `id -un`; os.getlogin() disagrees
    # under pytest (it reports the controlling terminal's owner).
    user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    label = f"com.{user}.mcp-agent-mail"
    _, loaded_dir, _ = _run_retirement(tmp_path, [label])
    assert not (loaded_dir / label).exists()


def test_nothing_is_touched_when_no_legacy_job_is_loaded(tmp_path: Path) -> None:
    """The null case: a clean machine must come through untouched."""
    out, _, log = _run_retirement(tmp_path, [])
    assert "retired" not in out
    assert "bootout" not in log.read_text()


def test_keep_legacy_mail_leaves_it_alone_but_says_so(tmp_path: Path) -> None:
    label = "org.agentstack.mcp-agent-mail"
    out, loaded_dir, log = _run_retirement(tmp_path, [label], keep_legacy=True)
    assert (loaded_dir / label).exists(), "the opt-out still retired the service"
    assert "bootout" not in log.read_text()
    assert "left alone" in out


def test_the_known_labels_can_be_overridden(tmp_path: Path) -> None:
    label = "com.example.some-other-mail"
    _, loaded_dir, _ = _run_retirement(
        tmp_path,
        [label],
        extra_env={"AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS": f"{label},org.unused"},
    )
    assert not (loaded_dir / label).exists()


def test_an_unrelated_service_is_not_retired(tmp_path: Path) -> None:
    """Only known mail labels. Booting out someone's editor would be a disaster."""
    label = "com.example.unrelated"
    _, loaded_dir, log = _run_retirement(tmp_path, [label])
    assert (loaded_dir / label).exists()
    assert "bootout" not in log.read_text()
