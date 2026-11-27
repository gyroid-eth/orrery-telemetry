"""Real Git, with synthetic WSL mount discovery and no live stack services."""

import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "hooks/project-context.sh"
CLEANUP = ROOT / "hooks/cleanup-child-agent.sh"


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args],
                                   text=True, stderr=subprocess.STDOUT).strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "Windows 上の repository"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
        "commit", "-q", "--allow-empty", "-m", "Fixture")
    return path.resolve()


def create_worktree(source, checkout, mount, *, system="Linux",
                    release="6.18-microsoft-standard-WSL2", common=None, old_git=False):
    env = dict(os.environ, FIXTURE_SYSTEM=system, FIXTURE_RELEASE=release,
               FIXTURE_MOUNT=mount, FIXTURE_COMMON=str(common or (source / ".git")),
               FIXTURE_OLD_GIT=str(int(old_git)))
    command = r'''
    set -euo pipefail
    . "$1"
    uname() {
        case "$1" in
            -s) printf '%s\n' "$FIXTURE_SYSTEM" ;;
            -r) printf '%s\n' "$FIXTURE_RELEASE" ;;
        esac
    }
    findmnt() {
        [[ "$1" == --target && "$2" == "$FIXTURE_COMMON" ]] || exit 7
        printf '%s\n' "$FIXTURE_MOUNT"
    }
    git() {
        if [[ "$FIXTURE_OLD_GIT" == 1 && "${3:-}" == worktree && "${4:-}" == add && "${5:-}" == -h ]]; then
            echo "usage: git worktree add [--lock]"
            return 129
        fi
        command git "$@"
    }
    agentstack_add_child_worktree "$2" "$3" "exp/$4" HEAD
    '''
    subprocess.run(["bash", "-c", command, "fixture", str(CONTEXT), str(source), str(checkout), checkout.name],
                   env=env, check=True, capture_output=True, text=True)


def run_exit_cleanup(tmp_path, cwd, checkout):
    install = tmp_path / "isolated"
    install.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("AGENTSTACK_") and key not in (
               "TMUX", "TMUX_PANE", "PROJECT_KEY", "CHILD_REGISTRATION_TOKEN")}
    env.update(HOME=str(install), AGENT_NAME=checkout.name,
               AGENTSTACK_HOME=str(install), AGENTSTACK_WORKTREE_ROOT=str(checkout.parent),
               AGENTSTACK_HOOKS_DIR=str(ROOT / "hooks"), AGENTSTACK_RUNTIME_DIR=str(install / "runtime"))
    subprocess.run(["bash", str(CLEANUP), checkout.name], cwd=cwd, env=env,
                   check=True, capture_output=True, text=True)


@pytest.mark.parametrize("from_checkout", [False, True])
def test_drvfs_worktree_survives_prune_until_child_exit(repo, tmp_path, from_checkout):
    checkout = tmp_path / "Linux worktrees" / "Fixture"
    create_worktree(repo, checkout, r"9p rw,aname=drvfs;path=C:\134,cache=0x5,access=client")
    assert "locked ORRERY WSL child Fixture: protect from Windows Git prune" in git(repo, "worktree", "list", "--porcelain")

    # Windows Git cannot open the recorded Linux checkout. Move only this
    # fixture aside to reproduce a prunable entry with a real git command.
    missing = checkout.with_name("temporarily invisible")
    checkout.rename(missing)
    try:
        git(repo, "worktree", "prune", "--expire", "now")
        assert f"worktree {checkout.resolve()}" in git(repo, "worktree", "list", "--porcelain")
    finally:
        missing.rename(checkout)

    run_exit_cleanup(tmp_path, checkout if from_checkout else tmp_path, checkout)
    report = git(repo, "worktree", "list", "--porcelain")
    assert "\nlocked" not in report and checkout.is_dir()
    checkout.rename(missing)
    try:
        report = git(repo, "worktree", "prune", "--expire", "now", "--dry-run", "--verbose")
        assert "Removing worktrees/Fixture" in report
    finally:
        missing.rename(checkout)


@pytest.mark.parametrize(("mount", "system", "release", "locked"), [
    ("drvfs rw", "Linux", "microsoft", True),
    ("9p rw,aname=tools;path=drvfs,cache=0x5", "Linux", "microsoft", False),
    ("9p rw,aname=drvfs-extra,cache=0x5", "Linux", "microsoft", False),
    ("ext4 rw,errors=remount-ro", "Linux", "microsoft", False),
    ("9p rw,aname=drvfs", "Linux", "6.18-linux", False),
    ("drvfs rw", "Darwin", "microsoft", False),
])
def test_only_wsl_drvfs_git_metadata_gets_a_lock(repo, tmp_path, mount, system, release, locked):
    checkout = tmp_path / "Fixture"
    create_worktree(repo, checkout, mount, system=system, release=release)
    assert ("\nlocked " in git(repo, "worktree", "list", "--porcelain")) is locked


def test_exit_keeps_a_user_lock(repo, tmp_path):
    checkout = tmp_path / "Fixture"
    create_worktree(repo, checkout, "ext4 rw")
    git(repo, "worktree", "lock", "--reason", "Operator lock", str(checkout))
    run_exit_cleanup(tmp_path, checkout, checkout)
    assert "locked Operator lock" in git(repo, "worktree", "list", "--porcelain")


def test_uses_common_git_directory_even_when_starting_from_a_linux_checkout(repo, tmp_path):
    source = (tmp_path / "Linux parent").resolve()
    git(repo, "worktree", "add", "--detach", str(source), "HEAD")
    checkout = tmp_path / "Fixture"
    create_worktree(source, checkout, "9p rw,aname=drvfs", common=repo / ".git")
    assert "locked ORRERY WSL child Fixture" in git(repo, "worktree", "list", "--porcelain")


def test_older_git_uses_separate_lock_with_reason(repo, tmp_path):
    checkout = tmp_path / "Fixture"
    create_worktree(repo, checkout, "9p rw,aname=drvfs", old_git=True)
    assert "locked ORRERY WSL child Fixture" in git(repo, "worktree", "list", "--porcelain")
