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
import plistlib
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
    if [[ -n "${BOOTOUT_FAILS:-}" ]]; then
      exit 77
    fi
    rm -f "$LOADED_DIR/$label"
    exit 0
    ;;
esac
exit 0
"""


def _legacy_wrapper(tmp_path: Path) -> Path:
    """The predecessor's wrapper script, in the layout the record describes."""
    directory = tmp_path / "mcp_agent_mail" / "scripts"
    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / "run_server_with_token.sh"
    wrapper.write_text("#!/bin/bash\nexec true\n", encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def _mail_executable(tmp_path: Path, name: str = "serve-http") -> Path:
    """A real file inside a real mail directory, as an install would leave it."""
    directory = tmp_path / "mcp_agent_mail" / ".venv" / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / name
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _write_plist(path: Path, label: str, *program: str) -> None:
    with path.open("wb") as handle:
        plistlib.dump({"Label": label, "ProgramArguments": list(program)}, handle)


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
        # A real plist: the installer extracts Label and ProgramArguments with
        # plutil, so a placeholder document would exercise nothing.
        _write_plist(agents / f"{label}.plist", label, str(_mail_executable(tmp_path)))

    log = tmp_path / "launchctl.log"
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path / "home"),
        "LAUNCHCTL_LOG": str(log),
        "LOADED_DIR": str(loaded_dir),
    }
    return env, loaded_dir, log


def _log_text(log: Path) -> str:
    """No log file at all means launchctl was never invoked."""
    return log.read_text() if log.exists() else ""


def _run_retirement(
    tmp_path: Path,
    loaded: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    retire: bool = True,
    twice: bool = False,
    harness: tuple[dict[str, str], Path, Path] | None = None,
) -> tuple[str, str, Path, Path]:
    """Source the installer and call the step directly, with no install running."""
    env, loaded_dir, log = harness if harness is not None else _harness(tmp_path, loaded)
    env.update(extra_env or {})
    install_dir = tmp_path / "agentstack"
    install_dir.mkdir(exist_ok=True)
    script = f"""
set -euo pipefail
# The installer runs its own argument parsing and main() at the bottom; source
# only the definitions by stopping before it acts.
INSTALL_SH={INSTALLER!s}
# Pull in every function under test by name. Extracting them one at a time by
# hand has silently dropped a helper twice, and a missing helper looks exactly
# like the feature not working.
for fn in wait_for_launchd_unload retire_legacy_mail_services legacy_mail_plist_looks_like_mail path_belongs_to_a_mail_install; do
  eval "$(awk -v fn="$fn" 'index($0, fn "() {{") == 1 {{ inside = 1 }} inside {{ print }} inside && $0 == "}}" {{ exit }}' "$INSTALL_SH")"
  declare -f "$fn" >/dev/null || {{ echo "missing helper: $fn" >&2; exit 90; }}
done
say() {{ echo "$@"; }}
warn() {{ echo "warn: $@"; }}
DRY_RUN={'true' if False else 'false'}
RETIRE_LEGACY_MAIL={'true' if retire else 'false'}
LABEL_PREFIX=org.agentstack
LABEL=org.agentstack.agentdashboard
MAIL_AUTOSTART_LABEL=org.agentstack.mail
die() {{ echo "die: $@" >&2; exit 1; }}
INSTALL_DIR={install_dir!s}
uname() {{ echo Darwin; }}
retire_legacy_mail_services
{"retire_legacy_mail_services" if twice else ""}
"""
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return result.stdout, result.stderr, loaded_dir, log


def test_the_label_older_installers_actually_used_is_retired(tmp_path: Path) -> None:
    label = "org.agentstack.mcp-agent-mail"
    out, _err, loaded_dir, log = _run_retirement(tmp_path, [label])
    assert not (loaded_dir / label).exists(), "the legacy job was left loaded"
    assert f"bootout gui/{os.getuid()}/{label}" in _log_text(log)
    assert "retired legacy mail service" in out
    parked = tmp_path / "agentstack" / "parked-launchd" / f"{label}.plist"
    assert parked.exists(), "the plist was deleted instead of parked"


def test_the_per_user_label_is_retired_too(tmp_path: Path) -> None:
    # The installer builds this label from `id -un`; os.getlogin() disagrees
    # under pytest (it reports the controlling terminal's owner).
    user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    label = f"com.{user}.mcp-agent-mail"
    _out, _err, loaded_dir, _log = _run_retirement(tmp_path, [label])
    assert not (loaded_dir / label).exists()


def test_retirement_scan_is_idempotent(tmp_path: Path) -> None:
    label = "org.agentstack.mcp-agent-mail"
    out, err, loaded_dir, log = _run_retirement(tmp_path, [label], twice=True)
    assert not err
    assert not (loaded_dir / label).exists()
    assert _log_text(log).count(f"bootout gui/{os.getuid()}/{label}") == 1
    assert out.count("retired legacy mail service") == 1


def test_nothing_is_touched_when_no_legacy_job_is_loaded(tmp_path: Path) -> None:
    """The null case: a clean machine must come through untouched."""
    out, _err, _loaded, log = _run_retirement(tmp_path, [])
    assert "retired" not in out
    assert "bootout" not in _log_text(log)


def test_retirement_is_opt_in(tmp_path: Path) -> None:
    """Stopping a service someone is running is the operator's call.

    The default reports the collision -- two servers means two databases -- and
    leaves the decision to a human.
    """
    label = "org.agentstack.mcp-agent-mail"
    out, err, loaded_dir, log = _run_retirement(tmp_path, [label], retire=False)
    assert (loaded_dir / label).exists(), "a previous service was retired without being asked"
    assert "bootout" not in _log_text(log)
    assert "--retire-legacy-mail" in (out + err)


def test_the_label_this_install_owns_can_never_be_retired(tmp_path: Path) -> None:
    """One stray environment variable must not stop the server being installed."""
    label = "org.orrery.mail"
    env, loaded_dir, log = _harness(tmp_path, [label])
    out, err, loaded_dir, log = _run_retirement(
        tmp_path,
        [],
        extra_env={"AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS": label},
        harness=(env, loaded_dir, log),
    )
    assert (loaded_dir / label).exists(), "the current production label was retired"
    assert "bootout" not in _log_text(log)
    assert "this install owns it" in (out + err)


def test_a_job_that_is_not_a_mail_service_is_left_alone(tmp_path: Path) -> None:
    """A supplied label is not evidence. Read what the job actually runs."""
    label = "com.example.editor"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        "/Applications/Editor.app/Contents/MacOS/editor",
    )
    out, err, loaded_dir, log = _run_retirement(
        tmp_path,
        [],
        extra_env={"AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS": label},
        harness=(env, loaded_dir, log),
    )
    assert (loaded_dir / label).exists(), "an unrelated service was retired"
    assert "bootout" not in _log_text(log)
    assert "does not look like a mail service" in (out + err)


def test_a_failed_bootout_stops_the_install_and_keeps_the_plist(tmp_path: Path) -> None:
    """Never report 'retired' while the job is still loaded.

    Moving the definition aside after a failed bootout leaves a running server
    with nothing to restore it from -- strictly worse than the collision.
    """
    label = "org.agentstack.mcp-agent-mail"
    out, err, loaded_dir, _log = _run_retirement(
        tmp_path, [label], extra_env={"BOOTOUT_FAILS": "1"}
    )
    assert (loaded_dir / label).exists()
    assert "retired legacy mail service" not in out
    assert "by hand" in err
    parked = tmp_path / "agentstack" / "parked-launchd" / f"{label}.plist"
    assert not parked.exists(), "the plist was parked even though the job is still loaded"
    still_there = tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist"
    assert still_there.exists()


def test_the_known_labels_can_be_overridden(tmp_path: Path) -> None:
    label = "com.example.some-other-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        str(_mail_executable(tmp_path, "serve")),
    )
    _out, _err, loaded_dir, _log = _run_retirement(
        tmp_path,
        [],
        extra_env={"AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS": f"{label},org.unused"},
        harness=(env, loaded_dir, log),
    )
    assert not (loaded_dir / label).exists()


def test_a_known_label_on_someone_elses_job_is_not_enough(tmp_path: Path) -> None:
    """The check must read what the job runs, not the document as a whole.

    Every legacy label we look for is itself written inside the plist, so a
    whole-document search confirms itself: an editor whose Label happens to be
    org.agentstack.mcp-agent-mail passed, and was booted out.
    """
    label = "org.agentstack.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        "/Applications/Editor.app/Contents/MacOS/editor",
    )
    out, err, loaded_dir, log = _run_retirement(
        tmp_path, [], harness=(env, loaded_dir, log)
    )
    assert (loaded_dir / label).exists(), "an editor carrying a known label was retired"
    assert "bootout" not in _log_text(log)
    assert "does not look like a mail service" in (out + err)


def test_an_unparseable_plist_fails_closed(tmp_path: Path) -> None:
    """Not being able to tell is not permission to act."""
    label = "org.agentstack.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    (tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist").write_text(
        "this is not a plist; mcp-agent-mail used to run here\n", encoding="utf-8"
    )
    _out, _err, loaded_dir, log = _run_retirement(
        tmp_path, [], harness=(env, loaded_dir, log)
    )
    assert (loaded_dir / label).exists()
    assert "bootout" not in _log_text(log)


def test_this_installs_own_dashboard_label_is_protected(tmp_path: Path) -> None:
    """Every label this install registers is protected, by value.

    Denying the whole prefix would be simpler and wrong: the legacy job this
    step exists to retire lives under the same prefix.
    """
    label = "org.agentstack.agentdashboard"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        str(_mail_executable(tmp_path, "agentstack-mail-service")),  # ours
    )
    out, err, loaded_dir, log = _run_retirement(
        tmp_path,
        [],
        extra_env={"AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS": label},
        harness=(env, loaded_dir, log),
    )
    assert (loaded_dir / label).exists(), "this install's own dashboard was retired"
    assert "this install owns it" in (out + err)


def test_an_existing_parked_copy_is_never_overwritten(tmp_path: Path) -> None:
    """The parked plist may be the only remaining definition of that service."""
    label = "org.agentstack.mcp-agent-mail"
    parked_dir = tmp_path / "agentstack" / "parked-launchd"
    parked_dir.mkdir(parents=True)
    keepsake = parked_dir / f"{label}.plist"
    keepsake.write_text("ORIGINAL RECOVERY COPY\n", encoding="utf-8")

    _out, _err, _loaded, _log = _run_retirement(tmp_path, [label])
    assert keepsake.read_text() == "ORIGINAL RECOVERY COPY\n", "the earlier copy was replaced"
    assert list(parked_dir.glob(f"{label}.plist.*")), "the new copy was not kept"


def test_a_symlinked_plist_is_refused(tmp_path: Path) -> None:
    """Moving the link parks a dangling file and reports success."""
    label = "org.agentstack.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    real = tmp_path / "elsewhere.plist"
    _write_plist(real, label, "/opt/mcp_agent_mail/serve")
    (tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist").symlink_to(real)

    _out, _err, loaded_dir, log = _run_retirement(
        tmp_path, [], harness=(env, loaded_dir, log)
    )
    assert (loaded_dir / label).exists()
    assert "bootout" not in _log_text(log)


def test_an_argument_that_mentions_mail_is_not_a_mail_service(tmp_path: Path) -> None:
    """Only the executable counts.

    An editor invoked with --note=/tmp/mcp-agent-mail-migration.txt is an
    editor. Matching the whole argument vector booted it out.
    """
    label = "org.agentstack.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        "/Applications/Editor.app/Contents/MacOS/editor",
        "--note=/tmp/mcp-agent-mail-migration.txt",
    )
    _out, _err, loaded_dir, log = _run_retirement(
        tmp_path, [], harness=(env, loaded_dir, log)
    )
    assert (loaded_dir / label).exists(), "an editor was retired for one of its arguments"
    assert "bootout" not in _log_text(log)


def test_the_mail_autostart_label_is_protected(tmp_path: Path) -> None:
    """Every label this install registers, including the autostart trigger."""
    label = "org.agentstack.mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        str(_mail_executable(tmp_path, "agentstack-mail-service")),
    )
    out, err, loaded_dir, log = _run_retirement(
        tmp_path,
        [],
        extra_env={"AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS": label},
        harness=(env, loaded_dir, log),
    )
    assert (loaded_dir / label).exists(), "this install's own autostart trigger was retired"
    assert "this install owns it" in (out + err)


def test_the_predecessor_recorded_in_the_cutover_document_is_retired(
    tmp_path: Path,
) -> None:
    """The definition this step exists for, verbatim from the sealed record.

    The cutover record pinned the predecessor's ProgramArguments as
    ["/bin/bash", ".../mcp_agent_mail/scripts/run_server_with_token.sh"]. An
    earlier fix looked only at argv[0], saw /bin/bash, and left the very job it
    was written to retire running -- with the tester's two-database split
    intact.
    """
    label = "com.example-operator.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        "/bin/bash",
        str(_legacy_wrapper(tmp_path)),
    )
    out, err, loaded_dir, log = _run_retirement(
        tmp_path,
        [],
        extra_env={"AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS": label},
        harness=(env, loaded_dir, log),
    )
    assert not (loaded_dir / label).exists(), f"the sealed predecessor survived: {out}{err}"
    assert "bootout" in _log_text(log)


def test_an_interpreter_running_something_else_is_left_alone(tmp_path: Path) -> None:
    """Recognising the wrapper form must not accept every shell script."""
    label = "org.agentstack.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        "/bin/bash",
        "/Users/someone/backup/nightly.sh",
    )
    _out, _err, loaded_dir, log = _run_retirement(
        tmp_path, [], harness=(env, loaded_dir, log)
    )
    assert (loaded_dir / label).exists()
    assert "bootout" not in _log_text(log)


def test_a_name_shaped_lookalike_is_not_retired(tmp_path: Path) -> None:
    """Exact basenames. "not-mcp-agent-mail-backup" is not this service."""
    label = "org.agentstack.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        "/Applications/not-mcp-agent-mail-backup",
    )
    _out, _err, loaded_dir, log = _run_retirement(
        tmp_path, [], harness=(env, loaded_dir, log)
    )
    assert (loaded_dir / label).exists()
    assert "bootout" not in _log_text(log)


def test_a_name_shaped_symlink_to_something_else_is_not_retired(
    tmp_path: Path,
) -> None:
    """Follow the name to what it actually runs."""
    label = "org.agentstack.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    lookalike = tmp_path / "mcp-agent-mail"
    lookalike.symlink_to("/bin/echo")
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        str(lookalike),
    )
    _out, _err, loaded_dir, log = _run_retirement(
        tmp_path, [], harness=(env, loaded_dir, log)
    )
    assert (loaded_dir / label).exists(), "a symlink to /bin/echo was retired as mail"
    assert "bootout" not in _log_text(log)


def test_a_mail_directory_in_the_path_does_not_vouch_for_a_symlink(
    tmp_path: Path,
) -> None:
    """Judge the canonical path, not the path as written.

    ".../mcp_agent_mail/editor -> /bin/echo" sits under a mail directory and
    runs an echo. So does ".../mcp_agent_mail/../editor".
    """
    label = "org.agentstack.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    directory = tmp_path / "mcp_agent_mail"
    directory.mkdir(parents=True, exist_ok=True)
    impostor = directory / "editor"
    impostor.symlink_to("/bin/echo")
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        str(impostor),
    )
    _out, _err, loaded_dir, log = _run_retirement(
        tmp_path, [], harness=(env, loaded_dir, log)
    )
    assert (loaded_dir / label).exists(), "a link to /bin/echo was retired as mail"
    assert "bootout" not in _log_text(log)


def test_a_traversal_out_of_a_mail_directory_is_not_a_mail_install(
    tmp_path: Path,
) -> None:
    label = "org.agentstack.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    (tmp_path / "mcp_agent_mail").mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "editor"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o755)
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        str(tmp_path / "mcp_agent_mail" / ".." / "editor"),
    )
    _out, _err, loaded_dir, log = _run_retirement(
        tmp_path, [], harness=(env, loaded_dir, log)
    )
    assert (loaded_dir / label).exists()
    assert "bootout" not in _log_text(log)


def test_an_interpreter_is_recognised_by_path_not_by_name(tmp_path: Path) -> None:
    """A shell script called python3 must not vouch for what follows it."""
    label = "org.agentstack.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    fake_interpreter = tmp_path / "python3"
    fake_interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_interpreter.chmod(0o755)
    script = tmp_path / "mcp_agent_mail" / "server.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('hi')\n", encoding="utf-8")
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        str(fake_interpreter),
        str(script),
    )
    _out, _err, loaded_dir, log = _run_retirement(
        tmp_path, [], harness=(env, loaded_dir, log)
    )
    assert (loaded_dir / label).exists(), "an impostor interpreter vouched for its argument"
    assert "bootout" not in _log_text(log)


def test_an_executable_nobody_can_run_is_not_a_running_service(tmp_path: Path) -> None:
    label = "org.agentstack.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    executable = _mail_executable(tmp_path)
    executable.chmod(0o000)
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        str(executable),
    )
    _out, _err, loaded_dir, log = _run_retirement(
        tmp_path, [], harness=(env, loaded_dir, log)
    )
    executable.chmod(0o755)  # so the temp dir can be cleaned up
    assert (loaded_dir / label).exists()
    assert "bootout" not in _log_text(log)


def test_a_differently_named_link_to_the_real_thing_is_recognised(
    tmp_path: Path,
) -> None:
    """The reverse direction: follow the link, judge what it lands on."""
    label = "org.agentstack.mcp-agent-mail"
    env, loaded_dir, log = _harness(tmp_path, [])
    (loaded_dir / label).write_text("loaded\n", encoding="utf-8")
    real = _mail_executable(tmp_path)
    entry = tmp_path / "mail-entry"
    entry.symlink_to(real)
    _write_plist(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist",
        label,
        str(entry),
    )
    _out, _err, loaded_dir, log = _run_retirement(
        tmp_path, [], harness=(env, loaded_dir, log)
    )
    assert not (loaded_dir / label).exists(), "a link to the real service was not recognised"


def test_no_function_is_defined_twice_in_the_installer() -> None:
    """The same hazard: bash takes the last definition of a name."""
    import collections
    import re

    names = re.findall(r"^([a-z_][a-z0-9_]*)\(\) \{", INSTALLER.read_text(), re.MULTILINE)
    duplicates = [name for name, count in collections.Counter(names).items() if count > 1]
    assert not duplicates, f"defined more than once: {duplicates}"
