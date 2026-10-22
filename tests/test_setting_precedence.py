"""Explicit value > the previous install's env.sh > the product default.

Issue #137: a re-install (`git pull && ./scripts/install.sh` in a new shell)
put the port, label prefix, terminal, MCP URL, Mail state and several other
settings back to their defaults, because the installer read them only from the
environment. Issue #33: `ags_load_env` sourced env.sh over a project key the
caller had set explicitly, so a top-level launcher registered under the
installed project instead.

Both follow one order, defined once in hooks/project-context.sh. Every test
here runs with a scrubbed environment and a temporary HOME, so none of them
depends on (or can touch) an install on the machine running them.
"""

from __future__ import annotations

import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"
CONTEXT = ROOT / "hooks" / "project-context.sh"
LAUNCH_LIB = ROOT / "bin" / "lib" / "agentstack-launch.sh"
# macOS still ships bash 3.2 as /bin/bash; the launchers must work there.
BASH = "/bin/bash"


def _scrubbed_env(home: pathlib.Path, **extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENTSTACK_") and key != "PROJECT_KEY"
    }
    env["HOME"] = str(home)
    env.update(extra)
    return env


def _fake_bin(root: pathlib.Path) -> pathlib.Path:
    fake_bin = root / "fake-bin"
    fake_bin.mkdir(parents=True)
    for name, body in {
        "uname": "#!/bin/sh\necho Linux\n",
        "tmux": "#!/bin/sh\nexit 0\n",
        "uv": "#!/bin/sh\nexit 0\n",
        "systemctl": "#!/bin/sh\nexit 0\n",
    }.items():
        path = fake_bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return fake_bin


def _write_env_sh_like_the_installer(home: pathlib.Path, values: dict[str, str]) -> None:
    """Produce env.sh with the installer's own write_env_file, not a lookalike."""
    text = INSTALLER.read_text(encoding="utf-8")
    start = text.index("write_env_file() {")
    end = text.index("\nPY\n}\n", start) + 6
    install_dir = home / ".agentstack"
    install_dir.mkdir(parents=True, exist_ok=True)
    assignments = "".join(
        f"{name}={shlex.quote(value)}\n" for name, value in values.items()
    )
    script = (
        "plan() { :; }\n"
        "DRY_RUN=false\n"
        f"PYTHON_BIN={shlex.quote(sys.executable)}\n"
        f"ENV_FILE={shlex.quote(str(install_dir / 'env.sh'))}\n"
        + assignments
        # The writer runs under this interpreter; PYTHON_BIN is only the value.
        + text[start:end].replace('"$PYTHON_BIN" - "$ENV_FILE"', f'{shlex.quote(sys.executable)} - "$ENV_FILE"', 1)
        + "write_env_file\n"
    )
    subprocess.run(
        ["bash", "-c", script],
        env=_scrubbed_env(home),
        check=True,
        capture_output=True,
        text=True,
    )


# The installer variable behind each env.sh name the fixture sets as a choice.
_CHOSEN_NAMES = {
    "PORT": "AGENTSTACK_PORT",
    "LABEL_PREFIX": "AGENTSTACK_LABEL_PREFIX",
    "MAIL_LAUNCHD_LABEL_SETTING": "AGENTSTACK_MAIL_LAUNCHD_LABEL",
    "TERMINAL": "AGENTSTACK_TERMINAL",
    "MCP_URL": "AGENTSTACK_MCP_URL",
    "PATH_VALUE": "AGENTSTACK_PATH",
    "PYTHON_BIN": "AGENTSTACK_PYTHON",
    "LANG_SETTING": "AGENTSTACK_LANG",
    "MURMUR_SETTING": "AGENTSTACK_MURMUR",
    "DELIVERABLE_ROOTS": "AGENTSTACK_DELIVERABLE_ROOTS",
    "VAULT_SETTING": "AGENTSTACK_VAULT",
    "CODEX_ADD_DIRS_SETTING": "AGENTSTACK_CODEX_ADD_DIRS",
    "MANAGED_AGENTS_FILE": "AGENTSTACK_MANAGED_AGENTS_FILE",
    "DASHBOARD_LOG": "AGENTSTACK_DASHBOARD_LOG",
    "DASHBOARD_LOG_MAX_BYTES": "AGENTSTACK_DASHBOARD_LOG_MAX_BYTES",
    "DASHBOARD_LOG_BACKUPS": "AGENTSTACK_DASHBOARD_LOG_BACKUPS",
    "DASHBOARD_RESTART_DELAY": "AGENTSTACK_DASHBOARD_RESTART_DELAY",
}


def _previous_install(
    tmp_path: pathlib.Path, *, not_chosen: tuple[str, ...] = (), **override: str
) -> tuple[pathlib.Path, dict[str, str]]:
    """env.sh as the installer writes it after a run that chose these values.

    ``not_chosen`` names installer variables whose value env.sh records but the
    user never chose (a default written out); ``override`` replaces values.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    chosen = {
        "PROJECT_KEY": str(project),
        "EXTRA_PROTECTED_ROOTS": "",
        "PORT": "19876",
        "LABEL_PREFIX": "org.agentstack.test.inherit",
        "MAIL_LAUNCHD_LABEL_SETTING": "org.agentstack.test.inherit.mail-service",
        "TERMINAL": "none",
        "MCP_URL": "http://127.0.0.1:1/mcp",
        "PATH_VALUE": "/custom/bin:/usr/bin:/bin",
        "PYTHON_BIN": sys.executable,
        "NATIVE_MAIL_STATE_ROOT": str(tmp_path / "mail-state"),
        "NATIVE_MAIL_SERVICE_ROOT": str(tmp_path / "mail-service"),
        # Derived values a real env.sh always carries next to the roots.
        "MAIL_DB": str(tmp_path / "mail-state" / "storage.sqlite3"),
        "MAIL_ENV": str(tmp_path / "mail-service" / "renders" / "old-render" / "service.env"),
        "LANG_SETTING": "ja",
        "MURMUR_SETTING": "off",
        "DELIVERABLE_ROOTS": str(tmp_path / "deliverables"),
        "VAULT_SETTING": str(tmp_path / "Vault With Spaces"),
        "CLAUDE_JSON": str(tmp_path / "claude.json"),
        "MANAGED_AGENTS_FILE": str(tmp_path / "managed.txt"),
        "DASHBOARD_LOG": str(tmp_path / "logs" / "dashboard.log"),
        "DASHBOARD_LOG_MAX_BYTES": "1048576",
        "DASHBOARD_LOG_BACKUPS": "7",
        "DASHBOARD_RESTART_DELAY": "11",
        "NATIVE_MAIL_MANAGEMENT_SOCKET": str(tmp_path / "mail.sock"),
        "CODEX_ADD_DIRS_SETTING": str(tmp_path / "secret"),
        **override,
    }
    chosen["CHOSEN_SETTINGS"] = " ".join(
        name for variable, name in _CHOSEN_NAMES.items() if variable not in not_chosen
    )
    _write_env_sh_like_the_installer(home, chosen)
    return home, chosen


def _dry_run(home: pathlib.Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = _scrubbed_env(home, **extra)
    fake = _fake_bin(pathlib.Path(tempfile.mkdtemp(dir=home.parent)))
    env["PATH"] = f"{fake}:{env['PATH']}"
    # Setting/plan tests must not contact a developer's live default-port Mail.
    (fake / "sitecustomize.py").write_text(
        "import errno, socket\n"
        "def blocked(*args, **kwargs): raise ConnectionRefusedError(errno.ECONNREFUSED, 'isolated installer fixture')\n"
        "socket.socket.connect = blocked\n"
        "socket.socket.connect_ex = lambda *args: errno.ECONNREFUSED\n"
        "socket.create_connection = blocked\n"
    )
    env["PYTHONPATH"] = f"{fake}:{env.get('PYTHONPATH', '')}"
    return subprocess.run(
        ["bash", str(INSTALLER), "--dashboard-only", "--dry-run", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


# --- #137: the installer -----------------------------------------------------


def test_reinstall_without_environment_keeps_every_non_default_setting(tmp_path):
    home, chosen = _previous_install(tmp_path)
    result = _dry_run(home)
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    assert f"project key: {chosen['PROJECT_KEY']}" in out
    assert "dashboard port: 19876" in out
    assert "label prefix: org.agentstack.test.inherit" in out
    assert "terminal: none" in out
    assert "ORRERY Mail MCP URL: http://127.0.0.1:1/mcp" in out
    assert f"python: {sys.executable}" in out and "from the existing env.sh" in out
    # The Mail database stays where it was instead of a fresh empty default.
    assert f"initialize empty ORRERY Mail state at {chosen['NATIVE_MAIL_STATE_ROOT']}" in out
    assert f"{chosen['NATIVE_MAIL_SERVICE_ROOT']}/candidates/" in out
    # The service is registered under the same label and probed on the same port.
    assert "org.agentstack.test.inherit.agentdashboard.service" in out
    assert "http://127.0.0.1:19876/api/agents" in out
    assert "org.agentstack.agentdashboard" not in out
    assert ":8770" not in out


def _resolve_like_the_installer(
    home: pathlib.Path,
    *outputs: str,
    args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> list[str]:
    """Run the installer itself, options and all, up to preflight; print values.

    For values the dry-run summary does not show. Everything before preflight
    runs unchanged except where the script finds its own directory.
    """
    text = INSTALLER.read_text(encoding="utf-8")
    locate = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
    assert text.count(locate) == 1
    head = text[: text.index('PREFLIGHT_OS=""')].replace(
        locate, f"SCRIPT_DIR={shlex.quote(str(ROOT / 'scripts'))}"
    )
    probe = head + "".join(f'printf "%s\\n" "${name}"\n' for name in outputs)
    # No codex on PATH: the codex probe in the same block has nothing to run.
    result = subprocess.run(
        ["bash", "-c", probe, "install.sh", *args],
        env=_scrubbed_env(home, PATH="/usr/bin:/bin", **(env or {})),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def _chosen(home: pathlib.Path, **kwargs) -> set[str]:
    (line,) = _resolve_like_the_installer(home, "CHOSEN_SETTINGS", **kwargs)
    return set(line.split())


def test_reinstall_resolves_the_settings_the_summary_does_not_show(tmp_path):
    home, chosen = _previous_install(tmp_path)
    assert _resolve_like_the_installer(
        home,
        "PATH_VALUE", "LANG_SETTING", "MURMUR_SETTING", "DELIVERABLE_ROOTS",
        "PYTHON_INHERITED", "MAIL_LAUNCHD_LABEL_SETTING",
        "NATIVE_MAIL_STATE_ROOT", "NATIVE_MAIL_SERVICE_ROOT",
    ) == [
        chosen["PATH_VALUE"],
        "ja",
        "off",
        chosen["DELIVERABLE_ROOTS"],
        sys.executable,
        chosen["MAIL_LAUNCHD_LABEL_SETTING"],
        chosen["NATIVE_MAIL_STATE_ROOT"],
        chosen["NATIVE_MAIL_SERVICE_ROOT"],
    ]
    # What was chosen before is still a choice, and recorded as one again.
    assert _chosen(home) == set(chosen["CHOSEN_SETTINGS"].split())


_USER_PATHS = (
    "VAULT_SETTING", "MANAGED_AGENTS_FILE", "DASHBOARD_LOG",
    "DASHBOARD_LOG_MAX_BYTES", "DASHBOARD_LOG_BACKUPS", "DASHBOARD_RESTART_DELAY",
    "NATIVE_MAIL_MANAGEMENT_SOCKET",
)


def test_vault_and_dashboard_settings_are_inherited(tmp_path):
    """A tester's AGENTSTACK_VAULT went back to empty on every re-install."""
    home, chosen = _previous_install(tmp_path)
    assert _resolve_like_the_installer(home, *_USER_PATHS) == [chosen[name] for name in _USER_PATHS]


def test_explicit_vault_and_dashboard_settings_win(tmp_path):
    home, _ = _previous_install(tmp_path)
    explicit = {
        "AGENTSTACK_VAULT": "/explicit/vault",
        "AGENTSTACK_MANAGED_AGENTS_FILE": "/explicit/managed.txt",
        "AGENTSTACK_DASHBOARD_LOG": "/explicit/dashboard.log",
        "AGENTSTACK_DASHBOARD_LOG_MAX_BYTES": "2",
        "AGENTSTACK_DASHBOARD_LOG_BACKUPS": "4",
        "AGENTSTACK_DASHBOARD_RESTART_DELAY": "6",
        "AGENTSTACK_MAIL_MANAGEMENT_SOCKET": "/explicit/mail.sock",
    }
    assert _resolve_like_the_installer(home, *_USER_PATHS, env=explicit) == list(explicit.values())


def test_reset_clears_vault_and_dashboard_settings_but_not_the_mail_socket(tmp_path):
    home, chosen = _previous_install(tmp_path)
    runtime = home / ".agentstack" / "runtime"
    assert _resolve_like_the_installer(home, *_USER_PATHS, args=("--reset-settings",)) == [
        "",
        f"{runtime}/managed_agents.txt",
        f"{runtime}/dashboard.log",
        "5242880",
        "3",
        "5",
        # Where Mail listens goes with where its state lives.
        chosen["NATIVE_MAIL_MANAGEMENT_SOCKET"],
    ]


def test_an_explicit_mail_state_root_does_not_inherit_the_old_socket(tmp_path):
    home, _ = _previous_install(tmp_path)
    assert _resolve_like_the_installer(
        home,
        "NATIVE_MAIL_MANAGEMENT_SOCKET",
        env={"AGENTSTACK_MAIL_STATE_ROOT": str(tmp_path / "other-state")},
    ) == [""]


def test_an_explicit_prefix_derives_the_mail_label_again(tmp_path):
    home, _ = _previous_install(tmp_path)
    assert _resolve_like_the_installer(
        home, "MAIL_LAUNCHD_LABEL_SETTING", args=("--label-prefix", "org.agentstack.test.other")
    ) == ["org.agentstack.test.other.mail-service"]
    # Back to the default prefix: back to the historical (empty) mail label.
    assert _resolve_like_the_installer(
        home, "MAIL_LAUNCHD_LABEL_SETTING", args=("--label-prefix", "org.agentstack")
    ) == [""]


# --- review of PR #146 --------------------------------------------------------


_MAIL_IDENTITY = (
    "LABEL_PREFIX", "MAIL_LAUNCHD_LABEL_SETTING", "MCP_URL",
    "NATIVE_MAIL_STATE_ROOT", "NATIVE_MAIL_SERVICE_ROOT", "NATIVE_MAIL_MANAGEMENT_SOCKET",
)


def test_p1_reset_keeps_the_mail_service_with_its_state(tmp_path):
    """--reset-settings kept the Mail state but put the Mail label and URL back.

    The old job then stayed registered under the old label, and a second Mail
    server was provisioned under the default label and URL for the same
    database. What the running services are called stays with where their
    data lives; the port (the same job, replaced in place) does not.
    """
    home, chosen = _previous_install(tmp_path)
    assert _resolve_like_the_installer(
        home, *_MAIL_IDENTITY, "PORT", "LANG_SETTING", args=("--reset-settings",)
    ) == [*(chosen[name] for name in _MAIL_IDENTITY), "8770", ""]


def test_p1_reset_dry_run_plans_the_same_services(tmp_path):
    home, chosen = _previous_install(tmp_path)
    result = _dry_run(home, "--reset-settings")
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    assert "label prefix: org.agentstack.test.inherit\n" in out
    assert "ORRERY Mail MCP URL: http://127.0.0.1:1/mcp" in out
    assert "org.agentstack.test.inherit.agentdashboard.service" in out
    assert "org.agentstack.test.inherit.mail.timer" in out
    assert "http://127.0.0.1:18765" not in out
    assert "org.agentstack.agentdashboard" not in out
    assert "dashboard port: 8770" in out


def test_p1_the_mail_identity_still_changes_when_given_explicitly(tmp_path):
    home, _ = _previous_install(tmp_path)
    assert _resolve_like_the_installer(
        home, "LABEL_PREFIX", "MAIL_LAUNCHD_LABEL_SETTING", "MCP_URL",
        args=("--reset-settings", "--label-prefix", "org.agentstack.test.moved"),
        env={"AGENTSTACK_MCP_URL": "http://127.0.0.1:2/mcp"},
    ) == [
        "org.agentstack.test.moved",
        "org.agentstack.test.moved.mail-service",
        "http://127.0.0.1:2/mcp",
    ]


def test_p2_1_an_explicit_empty_value_clears_only_that_setting(tmp_path):
    home, chosen = _previous_install(tmp_path)
    assert _resolve_like_the_installer(
        home,
        "VAULT_SETTING", "DELIVERABLE_ROOTS", "CODEX_ADD_DIRS_SETTING", "PORT",
        "LANG_SETTING", "TERMINAL",
        args=("--codex-add-dirs", ""),
        env={"AGENTSTACK_VAULT": "", "AGENTSTACK_DELIVERABLE_ROOTS": "", "AGENTSTACK_PORT": ""},
    ) == ["", "", "", "8770", "ja", "none"]
    # A cleared setting is back to its default, so it is no longer a choice.
    remaining = _chosen(
        home,
        args=("--codex-add-dirs", ""),
        env={"AGENTSTACK_VAULT": "", "AGENTSTACK_DELIVERABLE_ROOTS": "", "AGENTSTACK_PORT": ""},
    )
    assert remaining == set(chosen["CHOSEN_SETTINGS"].split()) - {
        "AGENTSTACK_VAULT", "AGENTSTACK_DELIVERABLE_ROOTS", "AGENTSTACK_CODEX_ADD_DIRS",
        "AGENTSTACK_PORT",
    }


def test_p2_1_clearing_the_codex_add_dirs_reaches_the_summary(tmp_path):
    home, chosen = _previous_install(tmp_path)
    result = _dry_run(home, "--codex-add-dirs", "")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "codex add dirs: (none beyond" in result.stdout
    assert chosen["CODEX_ADD_DIRS_SETTING"] not in result.stdout


def test_p2_2_a_default_env_sh_wrote_out_is_not_pinned(tmp_path):
    """A written-out default follows the current default on the next install."""
    home, _ = _previous_install(
        tmp_path,
        not_chosen=("PATH_VALUE", "PYTHON_BIN", "DASHBOARD_LOG_MAX_BYTES"),
        PATH_VALUE="/an/older/default/bin:/usr/bin:/bin",
        DASHBOARD_LOG_MAX_BYTES="1048576",
    )
    assert _resolve_like_the_installer(
        home, "PATH_VALUE", "PYTHON_INHERITED", "DASHBOARD_LOG_MAX_BYTES", "PORT"
    ) == [
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "",
        "5242880",
        "19876",
    ]


def test_p2_2_only_explicit_values_are_recorded_as_choices(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    assert _chosen(home, args=("--project-key", str(project))) == set()
    assert _chosen(
        home, args=("--project-key", str(project), "--port", "19000"),
        env={"AGENTSTACK_PYTHON": sys.executable},
    ) == {"AGENTSTACK_PORT", "AGENTSTACK_PYTHON"}


def test_p2_2_an_env_sh_from_before_the_record_keeps_what_differs_from_defaults(tmp_path):
    """env.sh written by 2026.09.30.4 and earlier has no record of choices."""
    home = tmp_path / "home"
    (home / ".agentstack").mkdir(parents=True)
    (home / ".agentstack" / "env.sh").write_text(
        f"export AGENTSTACK_PROJECT_KEY={tmp_path}\n"
        "export AGENTSTACK_PORT=19876\n"
        "export AGENTSTACK_CODEX_CHILD_APPROVAL=never\n"
        "export AGENTSTACK_DASHBOARD_LOG_MAX_BYTES=5242880\n"
        "export AGENTSTACK_PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/codex/bin\n"
        f"export AGENTSTACK_PYTHON={sys.executable}\n",
        encoding="utf-8",
    )
    assert _resolve_like_the_installer(
        home, "PORT", "PATH_VALUE", "PYTHON_INHERITED", "CHOSEN_SETTINGS"
    ) == [
        "19876",
        # PATH and Python were always resolved by the installer: never a choice
        # it can recognise, so they are resolved again.
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "",
        "AGENTSTACK_PORT",
    ]


def test_p2_2_values_echoed_from_a_sourced_env_sh_are_not_new_choices(tmp_path):
    """cockpit's update.sh (and a shell profile) export every env.sh value."""
    home, chosen = _previous_install(tmp_path, not_chosen=("PATH_VALUE",))
    env_sh = (home / ".agentstack" / "env.sh").read_text(encoding="utf-8")
    echoed = {}
    for line in env_sh.splitlines():
        if line.startswith("export "):
            name, _, raw = line[len("export "):].partition("=")
            (value,) = shlex.split(raw) or [""]
            echoed[name] = value
    assert _resolve_like_the_installer(
        home, "PATH_VALUE", "PORT", env=echoed
    ) == ["/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "19876"]
    assert _chosen(home, env=echoed) == set(chosen["CHOSEN_SETTINGS"].split())
    # Under --reset-settings an echo is not a request to keep the value.
    assert _resolve_like_the_installer(
        home, "PORT", "LANG_SETTING", "LABEL_PREFIX", args=("--reset-settings",), env=echoed
    ) == ["8770", "", chosen["LABEL_PREFIX"]]
    # An option typed on the command line is a request, echo or not.
    assert _resolve_like_the_installer(
        home, "PORT", args=("--reset-settings", "--port", "19876"), env=echoed
    ) == ["19876"]


def test_n1_values_given_every_time_since_before_the_record_stay_put(tmp_path):
    """Someone who has always passed AGENTSTACK_PATH / AGENTSTACK_PYTHON.

    Their env.sh predates the record of choices, so a value equal to it cannot
    be told apart from an echo. Taking it as an echo sent it back to the
    default, and the next install (now differing from env.sh) took it again:
    the value flipped on every re-install (re-check of #146, N-1).
    """
    home = tmp_path / "home"
    (home / ".agentstack").mkdir(parents=True)
    custom_path = "/custom/bin:/usr/bin:/bin"
    (home / ".agentstack" / "env.sh").write_text(
        f"export AGENTSTACK_PROJECT_KEY={tmp_path}\n"
        f"export AGENTSTACK_PATH={custom_path}\n"
        f"export AGENTSTACK_PYTHON={sys.executable}\n",
        encoding="utf-8",
    )
    given = {"AGENTSTACK_PATH": custom_path, "AGENTSTACK_PYTHON": sys.executable}
    outputs = ("PATH_VALUE", "PYTHON_REQUESTED", "PYTHON_INHERITED", "CHOSEN_SETTINGS")
    runs = []
    for _ in range(3):
        path_value, requested, inherited, chosen = _resolve_like_the_installer(home, *outputs, env=given)
        runs.append((path_value, requested or inherited, set(chosen.split())))
        # What this install would write for the next one.
        _write_env_sh_like_the_installer(home, {
            "PROJECT_KEY": str(tmp_path), "PATH_VALUE": path_value,
            "PYTHON_BIN": requested or inherited, "CHOSEN_SETTINGS": chosen,
        })
    assert runs == [(custom_path, sys.executable, {"AGENTSTACK_PATH", "AGENTSTACK_PYTHON"})] * 3


def test_p2_3_the_launchd_plist_escapes_every_value(tmp_path):
    import plistlib

    text = INSTALLER.read_text(encoding="utf-8")
    function = text[text.index("render_launchd_plist() {"):text.index("render_systemd_unit() {")]
    awkward = f'{tmp_path}/Research & Notes "quoted" <angled>'
    values = {
        "DRY_RUN": "false", "PYTHON_BIN": sys.executable, "REPO_ROOT": str(ROOT),
        "LABEL": "org.agentstack.test.plist.agentdashboard",
        "LABEL_PREFIX": "org.agentstack.test.plist", "VAULT_SETTING": awkward,
        "PROJECT_KEY": awkward, "CODEX_CHILD_CONFIG_OVERLAY_SETTING": awkward,
    }
    script = (
        "plan() { :; }\n"
        + "".join(f"{name}={shlex.quote(value)}\n" for name, value in values.items())
        + function
        + "render_launchd_plist\n"
    )
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["bash", "-c", script],
        env=_scrubbed_env(home, AGENTSTACK_CLAUDE_MODELS="a&b"),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    plist = home / "Library" / "LaunchAgents" / "org.agentstack.test.plist.agentdashboard.plist"
    with plist.open("rb") as handle:
        document = plistlib.load(handle)
    variables = document["EnvironmentVariables"]
    assert variables["AGENTSTACK_VAULT"] == awkward
    assert variables["AGENTSTACK_PROJECT_KEY"] == awkward
    assert variables["AGENTSTACK_CODEX_CHILD_CONFIG_OVERLAY"] == awkward
    # Escaped once, not twice.
    assert variables["AGENTSTACK_CLAUDE_MODELS"] == "a&b"
    if shutil.which("plutil"):
        lint = subprocess.run(["plutil", "-lint", str(plist)], capture_output=True, text=True)
        assert lint.returncode == 0, lint.stdout + lint.stderr


def test_p2_4_the_claude_json_override_is_not_inherited(tmp_path):
    """A one-off sandbox path must not keep receiving the MCP entry."""
    home, chosen = _previous_install(tmp_path, CLAUDE_JSON=str(tmp_path / "sandbox.json"))
    assert _resolve_like_the_installer(home, "CLAUDE_JSON") == [str(home / ".claude.json")]
    assert _resolve_like_the_installer(
        home, "CLAUDE_JSON", env={"AGENTSTACK_CLAUDE_JSON": "/explicit.json"}
    ) == ["/explicit.json"]


def test_explicit_options_and_environment_win_over_env_sh(tmp_path):
    home, _ = _previous_install(tmp_path)
    result = _dry_run(
        home,
        "--label-prefix",
        "org.agentstack.test.explicit",
        "--terminal",
        "ghostty",
        AGENTSTACK_PORT="19877",
        AGENTSTACK_MCP_URL="http://127.0.0.1:2/mcp",
        AGENTSTACK_PYTHON=sys.executable,
    )
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    assert "dashboard port: 19877" in out
    assert "label prefix: org.agentstack.test.explicit" in out
    assert "terminal: ghostty" in out
    assert "ORRERY Mail MCP URL: http://127.0.0.1:2/mcp" in out
    # AGENTSTACK_PYTHON equals what env.sh recorded: an echo, not a new choice.
    assert "org.agentstack.test.explicit.agentdashboard.service" in out
    assert "org.agentstack.test.inherit" not in out


def test_reset_settings_returns_to_defaults_but_keeps_project_and_mail_state(tmp_path):
    home, chosen = _previous_install(tmp_path)
    for args, extra in (
        (("--reset-settings",), {}),
        ((), {"AGENTSTACK_RESET_SETTINGS": "1"}),
    ):
        result = _dry_run(home, *args, **extra)
        out = result.stdout
        assert result.returncode == 0, out + result.stderr
        assert "settings: reset (not inherited from the existing env.sh)" in out
        assert "dashboard port: 8770" in out
        assert "terminal: auto" in out
        assert "; from the existing env.sh)" not in out
        # Where the data lives, and what its services are called, is not a
        # preference: still inherited.
        assert "label prefix: org.agentstack.test.inherit\n" in out
        assert "ORRERY Mail MCP URL: http://127.0.0.1:1/mcp" in out
        assert f"project key: {chosen['PROJECT_KEY']}" in out
        assert chosen["NATIVE_MAIL_STATE_ROOT"] in out


def test_reset_settings_rejects_a_value_it_does_not_understand(tmp_path):
    home, _ = _previous_install(tmp_path)
    result = _dry_run(home, AGENTSTACK_RESET_SETTINGS="yes")
    assert result.returncode == 2
    assert "AGENTSTACK_RESET_SETTINGS must be 0 or 1" in result.stderr


def test_a_recorded_python_that_is_gone_falls_back_to_the_search(tmp_path):
    home, _ = _previous_install(tmp_path)
    env_sh = home / ".agentstack" / "env.sh"
    env_sh.write_text(
        re.sub(
            r"^export AGENTSTACK_PYTHON=.*$",
            f"export AGENTSTACK_PYTHON={tmp_path}/gone/python3",
            env_sh.read_text(encoding="utf-8"),
            flags=re.M,
        ),
        encoding="utf-8",
    )
    result = _dry_run(home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the Python recorded in the existing env.sh" in result.stdout
    assert "searching again" in result.stdout


def test_first_install_still_uses_product_defaults(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    result = _dry_run(home, "--project-key", str(project), AGENTSTACK_PYTHON=sys.executable)
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    assert "dashboard port: 8770" in out
    assert "label prefix: org.agentstack\n" in out
    assert "terminal: auto" in out


# Written to env.sh but derived by the installer every time, never chosen:
# inheriting them would pin a value the installer is meant to recompute.
_DERIVED = {
    "AGENTSTACK_MAIL_DB",  # state root + storage.sqlite3
    "AGENTSTACK_MAIL_ENV",  # the current render
    "AGENTSTACK_MAIL_HOME",  # the state root
    "AGENTSTACK_SIGNALS_DIR",  # state root + signals
    "AGENTSTACK_MAIL_ENROLL_BIN",  # the adopted deployment
    "AGENTSTACK_MAIL_HTTP_BEARER_MODE",  # always disabled
    "AGENTSTACK_HOOKS_DIR",  # install dir
    "AGENTSTACK_SKILLS_DIR",  # install dir
    "AGENTSTACK_RUNTIME_DIR",  # install dir
    "AGENTSTACK_PERSISTENT_PROFILES_DIR",  # install dir
    "AGENTSTACK_CHOSEN_SETTINGS",  # the record of which settings were chosen
    # A sandbox override for tests and trials, not a setting (review P2-4): a
    # later plain install writes the real ~/.claude.json again.
    "AGENTSTACK_CLAUDE_JSON",
}


def test_every_name_env_sh_records_is_inherited_or_derived():
    """A new env.sh name has to be classified, or a re-install silently resets it."""
    context = CONTEXT.read_text(encoding="utf-8")
    listed = set(re.search(r'AGENTSTACK_INHERITED_SETTINGS="(.*?)"', context, re.S).group(1).split())
    installer = INSTALLER.read_text(encoding="utf-8")
    start = installer.index("write_env_file() {")
    written = set(re.findall(r'"(AGENTSTACK_[A-Z0-9_]+)":', installer[start:installer.index("\nPY\n}\n", start)]))
    assert written - listed - _DERIVED == set()
    assert listed & _DERIVED == set()


def test_every_inherited_setting_is_one_the_installer_records():
    """A name the installer never writes could never be inherited."""
    context = CONTEXT.read_text(encoding="utf-8")
    listed = re.search(r'AGENTSTACK_INHERITED_SETTINGS="(.*?)"', context, re.S)
    assert listed is not None
    names = listed.group(1).split()
    installer = INSTALLER.read_text(encoding="utf-8")
    start = installer.index("write_env_file() {")
    written = set(re.findall(r'"(AGENTSTACK_[A-Z0-9_]+)":', installer[start:installer.index("\nPY\n}\n", start)]))
    assert names and set(names) <= written, sorted(set(names) - written)


# --- #33: the launchers -----------------------------------------------------


def _home_with_installed_env(tmp_path: pathlib.Path) -> pathlib.Path:
    home = tmp_path / "home"
    (home / ".agentstack").mkdir(parents=True)
    (home / ".agentstack" / "env.sh").write_text(
        "export AGENTSTACK_PROJECT_KEY=/installed/StudyPlanner\n"
        "export AGENTSTACK_PROTECTED_ROOTS=/installed/StudyPlanner\n"
        "export AGENTSTACK_CODEX_BIN=/installed/codex\n"
        "export AGENTSTACK_HOOKS_DIR=/installed/hooks\n",
        encoding="utf-8",
    )
    return home


def _load_env(home: pathlib.Path, lib: pathlib.Path, script: str, **extra: str) -> list[str]:
    result = subprocess.run(
        [BASH, "-c", f"set -euo pipefail\n. {shlex.quote(str(lib))}\n{script}"],
        env=_scrubbed_env(home, **extra),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_contributor_repro_explicit_project_key_survives_ags_load_env(tmp_path):
    """The reproduction from #33 (2026-09-30), unchanged."""
    home = _home_with_installed_env(tmp_path)
    lines = _load_env(
        home,
        LAUNCH_LIB,
        'export AGENTSTACK_PROJECT_KEY=~/Developer/orrery-telemetry\n'
        'echo "before=$AGENTSTACK_PROJECT_KEY"\n'
        "ags_load_env\n"
        'echo "after=$AGENTSTACK_PROJECT_KEY"\n'
        'echo "roots=${AGENTSTACK_PROTECTED_ROOTS-unset}"\n',
    )
    explicit = f"{home}/Developer/orrery-telemetry"
    assert lines == [f"before={explicit}", f"after={explicit}", "roots=unset"]


def test_without_an_explicit_project_key_env_sh_supplies_it(tmp_path):
    home = _home_with_installed_env(tmp_path)
    lines = _load_env(
        home,
        LAUNCH_LIB,
        "ags_load_env\n"
        'echo "$AGENTSTACK_PROJECT_KEY"\n'
        'echo "${AGENTSTACK_PROTECTED_ROOTS-unset}"\n'
        'echo "$AGENTSTACK_CODEX_BIN"\n',
    )
    assert lines == ["/installed/StudyPlanner", "unset", "/installed/codex"]


def test_other_explicit_settings_survive_and_install_paths_still_load(tmp_path):
    home = _home_with_installed_env(tmp_path)
    lines = _load_env(
        home,
        LAUNCH_LIB,
        "ags_load_env\n"
        'echo "$AGENTSTACK_PROJECT_KEY"\n'
        'echo "$AGENTSTACK_PROTECTED_ROOTS"\n'
        'echo "$AGENTSTACK_CODEX_BIN"\n'
        'echo "$AGENTSTACK_HOOKS_DIR"\n',
        PROJECT_KEY="/legacy/live",
        AGENTSTACK_PROTECTED_ROOTS="/a:/b",
        AGENTSTACK_CODEX_BIN="/explicit/codex",
    )
    assert lines == ["/legacy/live", "/a:/b", "/explicit/codex", "/installed/hooks"]


def test_the_installed_layout_finds_the_shared_order(tmp_path):
    """Installed launchers live in $AGENTSTACK_HOME/bin, the order in hooks/."""
    home = _home_with_installed_env(tmp_path)
    install_dir = home / ".agentstack"
    (install_dir / "bin" / "lib").mkdir(parents=True)
    (install_dir / "hooks").mkdir()
    shutil.copy2(LAUNCH_LIB, install_dir / "bin" / "lib" / "agentstack-launch.sh")
    shutil.copy2(CONTEXT, install_dir / "hooks" / "project-context.sh")
    shutil.copy2(ROOT / "hooks/installed-env.py", install_dir / "hooks/installed-env.py")
    lines = _load_env(
        home,
        install_dir / "bin" / "lib" / "agentstack-launch.sh",
        'ags_load_env\necho "$AGENTSTACK_PROJECT_KEY"\n',
        AGENTSTACK_PROJECT_KEY="/explicit",
    )
    assert lines == ["/explicit"]


def test_every_launcher_loads_env_sh_only_through_ags_load_env():
    for name in ("agent-start", "agent-start-codex", "agent-start-gemini"):
        text = (ROOT / "bin" / name).read_text(encoding="utf-8")
        assert '\nags_load_env "$@"\n' in text, name
        assert "env.sh\"" not in text and "/env.sh" not in text, name


# --- Installed-file-only protection migration -------------------------------

EXTRAS = "AGENTSTACK_EXTRA_PROTECTED_ROOTS"
LEGACY = "AGENTSTACK_PROTECTED_ROOTS"
LITERAL_READER = ROOT / "hooks/installed-env.py"


def _protection_file(home, assignments):
    path = home / ".agentstack/env.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"export AGENTSTACK_PROJECT_KEY={shlex.quote(str(home / 'project'))}\n"
        + "".join(f"export {name}={shlex.quote(value)}\n" for name, value in assignments.items())
    )
    return path


def _protection_probe(home, env=None, args=()):
    """Run actual installer resolution, stopping before unrelated preflights."""
    text = INSTALLER.read_text()
    text = text[:text.index('case "$RESET_SETTINGS" in')]
    locate = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
    text = text.replace(locate, f"SCRIPT_DIR={shlex.quote(str(ROOT / 'scripts'))}")
    names = ("EXTRA_PROTECTED_ROOTS", "EXTRA_PROTECTED_ROOTS_SOURCE", "PROTECTION_MIGRATION",
             "LEGACY_PROTECTED_ROOTS", "PRESERVE_LEGACY_PROTECTED_ROOTS")
    text += "\n" + "".join(f'printf "%s\\0" "${name}"\n' for name in names)
    return subprocess.run([BASH, "-c", text, "install.sh", *args],
                          env=_scrubbed_env(home, **(env or {})), text=True, capture_output=True)


@pytest.mark.parametrize("saved,live,expected,migrate", [
    ({}, {}, "", False),
    ({}, {LEGACY: "/ambient/cache"}, "", False),
    ({LEGACY: "/vault:/project:/vault"}, {}, "/vault:/project:/vault", True),
    ({LEGACY: ""}, {}, "", True),
    ({LEGACY: "/saved"}, {LEGACY: "/ambient/cache"}, "/saved", True),
    ({LEGACY: "/saved", EXTRAS: "/extra"}, {}, "/extra", False),
    ({LEGACY: "/saved", EXTRAS: ""}, {}, "", False),
    ({LEGACY: "/saved"}, {EXTRAS: "/override"}, "/override", False),
    ({LEGACY: "/saved"}, {EXTRAS: ""}, "", False),
    ({LEGACY: "/saved", EXTRAS: "/old"}, {EXTRAS: ""}, "", False),
    ({LEGACY: "/saved", EXTRAS: ""}, {EXTRAS: "/override"}, "/override", False),
])
def test_protection_migration_precedence_is_presence_aware(tmp_path, saved, live, expected, migrate):
    _protection_file(tmp_path, saved)
    result = _protection_probe(tmp_path, live)
    assert result.returncode == 0, result.stderr
    values = result.stdout.split("\0")
    assert values[0] == expected
    assert values[2] == str(migrate).lower()
    assert values[3] == saved.get(LEGACY, "")
    assert values[4] == str(LEGACY in saved).lower()


@pytest.mark.parametrize("extras", [None, "", "/extra"])
def test_different_nonempty_ambient_roots_warn_even_when_extras_exists(tmp_path, extras):
    values = {LEGACY: "/installed"}
    if extras is not None:
        values[EXTRAS] = extras
    _protection_file(tmp_path, values)
    result = _protection_probe(tmp_path, {LEGACY: "/ambient"})
    assert result.returncode == 0, result.stderr
    assert "ambient AGENTSTACK_PROTECTED_ROOTS=/ambient differs" in result.stderr
    assert "never migrated" in result.stderr
    equal = _protection_probe(tmp_path, {LEGACY: "/installed"})
    assert "ambient AGENTSTACK_PROTECTED_ROOTS" not in equal.stderr


@pytest.mark.parametrize("value", ["", "/vault:/project:/vault", "/a space/O'Brien\\back\\:/$literal/$(text)/&<>%"])
def test_migration_writes_literal_order_once_and_keeps_legacy_compatibility(tmp_path, value):
    path = _protection_file(tmp_path, {LEGACY: value})
    for index in range(3):
        result = _protection_probe(tmp_path, args=("--reset-settings",))
        assert result.returncode == 0, result.stderr
        extras, source, migrating, legacy, preserve, _ = result.stdout.split("\0")
        assert extras == legacy == value
        assert migrating == ("true" if index == 0 else "false")
        _write_env_sh_like_the_installer(tmp_path, {
            "PROJECT_KEY": str(tmp_path / "project"), "EXTRA_PROTECTED_ROOTS": extras,
            "LEGACY_PROTECTED_ROOTS": legacy, "PRESERVE_LEGACY_PROTECTED_ROOTS": preserve,
        })
        assert path.stat().st_mode & 0o777 == 0o600
        for name in (EXTRAS, LEGACY):
            parsed = subprocess.run([sys.executable, str(LITERAL_READER), "value", name, str(path)],
                                    capture_output=True, text=True)
            assert parsed.returncode == 0, parsed.stderr
            assert parsed.stdout == value
        assert not list(path.parent.glob("env.sh.*"))


def test_fresh_writer_persists_empty_marker_without_ambient_roots(tmp_path):
    _write_env_sh_like_the_installer(tmp_path, {"PROJECT_KEY": str(tmp_path), "EXTRA_PROTECTED_ROOTS": ""})
    text = (tmp_path / ".agentstack/env.sh").read_text()
    assert f"export {EXTRAS}=''\n" in text
    assert f"export {LEGACY}=" not in text


def test_migration_dry_run_reports_source_value_destination_without_writes(tmp_path):
    home = tmp_path / "home"
    path = _protection_file(home, {LEGACY: "/vault:/repo"})
    before = path.read_bytes()
    result = _dry_run(home, AGENTSTACK_PYTHON=sys.executable)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"source={path}:{LEGACY} value=/vault:/repo destination={path}:{EXTRAS}" in result.stdout
    assert "would migrate installed" in result.stdout
    assert path.read_bytes() == before
    assert not (home / ".agentstack/install-state.json").exists()
    assert result.stdout.splitlines()[-1].startswith("mail-plan:")
    assert result.stderr.count("restart affected coordinated sessions together") == 2


@pytest.mark.parametrize("body", [
    "export {name}=/one\nexport {name}=/two\n",
    "export {name}=$HOME/vault\n",
    'export {name}="${{HOME}}/vault"\n',
    "export {name}=$(touch SENTINEL)\n",
    "export {name}=`touch SENTINEL`\n",
    "export {name}=/safe; touch SENTINEL\n",
    "export {name}=/two words\n",
    "export {name}='/multiline\n/value'\n",
    "export {name}=/continued\\\n/value\n",
    "if true; then export {name}='/vault'; fi\n",
    "if true; then\nexport {name}='/vault'\nfi\n",
    "readonly {name}='/vault'\n",
    "export -n {name}='/vault'\n",
    "export OTHER='one\nexport {name}=/fake\nthree'\n",
    "export OTHER=x; export {name}=/vault\n",
    "{name}=/vault\n",
    "export {name}=/glob/*\n",
    "export AGENTSTACK_'PROTECTED_ROOTS'='/vault'\n",
    "export AGENTSTACK_PROTECTED\\_ROOTS='/vault'\n",
])
@pytest.mark.parametrize("name", [EXTRAS, LEGACY])
def test_invalid_protection_is_not_absence_and_never_overwrites_or_executes(tmp_path, body, name):
    path = _protection_file(tmp_path, {})
    sentinel = tmp_path / "executed"
    path.write_text(path.read_text() + body.format(name=name).replace("SENTINEL", str(sentinel)))
    before = path.read_bytes()
    result = _protection_probe(tmp_path, {EXTRAS: "/explicit"})
    assert result.returncode == 2, result.stdout + result.stderr
    assert "invalid " in result.stderr
    assert path.read_bytes() == before
    assert not sentinel.exists()
    assert not (path.parent / "install-state.json").exists()


@pytest.mark.parametrize("rhs,value", [
    ("", ""), ("''", ""), ('""', ""), ("/unquoted", "/unquoted"),
    ("'/space and back\\slash'", "/space and back\\slash"),
    (shlex.quote("/O'Brien/\\back"), "/O'Brien/\\back"),
    ('"/escaped/\\$HOME/\\`text\\`"', "/escaped/$HOME/`text`"),
])
def test_literal_reader_distinguishes_empty_value_and_absence(tmp_path, rhs, value):
    path = tmp_path / "env.sh"
    path.write_text(f"export {LEGACY}={rhs}\n")
    state = subprocess.run([sys.executable, str(LITERAL_READER), "state", LEGACY, str(path)],
                           capture_output=True, text=True, check=True)
    assert state.stdout.strip() == ("present-value" if value else "present-empty")
    result = subprocess.run([sys.executable, str(LITERAL_READER), "value", LEGACY, str(path)],
                            capture_output=True, text=True, check=True)
    assert result.stdout == value
    absent = subprocess.run([sys.executable, str(LITERAL_READER), "state", EXTRAS, str(path)],
                            capture_output=True, text=True, check=True)
    assert absent.stdout == "absent\n"


def _installer_functions(*names):
    text = INSTALLER.read_text()
    chunks = []
    for name in names:
        start = text.index(name + "() {")
        following = re.search(r"\n[_a-zA-Z][_a-zA-Z0-9]*\(\) \{", text[start:])
        end = start + 1 + following.start() if following else len(text)
        chunks.append(text[start:end])
    return "\n".join(chunks)


@pytest.mark.parametrize("writer", ["launchd", "systemd", "manifest", "agentctl"])
def test_protection_renderers_preserve_literal_extras_as_data(tmp_path, writer):
    import json
    import plistlib

    value = '/vault/O\'Brien\\folder & <angles> "quotes" %h:$literal:$(not-run)'
    values = {
        "HOME": str(tmp_path), "DRY_RUN": "false", "PYTHON_BIN": sys.executable,
        "PYTHON": sys.executable, "REPO_ROOT": str(ROOT), "SCRIPT_DIR": str(ROOT / "scripts"), "LABEL": "test",
        "LABEL_PREFIX": "test", "EXTRA_PROTECTED_ROOTS": value,
        "LEGACY_PROTECTED_ROOTS": "/legacy", "PRESERVE_LEGACY_PROTECTED_ROOTS": "true",
        "INSTALL_DIR": str(tmp_path / "install"), "MANIFEST": str(tmp_path / "manifest.json"),
        "SAFE_MERGE_RESULT_FILE": str(tmp_path / "absent-merge"),
        "MCP_MERGE_RESULT_FILE": str(tmp_path / "absent-mcp"),
        "AGENT_MAIL_NAME_CAPABILITY_JSON": "{}", "CLAUDE_SKILLS_DIR": str(tmp_path / "skills"),
    }
    if writer == "agentctl":
        text = (ROOT / "dashboard/agentctl.sh").read_text()
        body = text[text.index("sed_escape() {"):text.index("background_pid() {")]
        values.update(PLIST_DST=str(tmp_path / "result.plist"),
                      PLIST_TEMPLATE=str(ROOT / "dashboard/agentdashboard.plist.template"))
        invoke = "render_plist"
    else:
        function = {"launchd": "render_launchd_plist", "systemd": "render_systemd_unit", "manifest": "write_manifest"}[writer]
        body = _installer_functions(function)
        invoke = function + (" manual ''" if writer == "manifest" else "")
    script = "plan() { :; }\n" + "".join(f"{key}={shlex.quote(val)}\n" for key, val in values.items()) + body + "\n" + invoke
    result = subprocess.run([BASH, "-e", "-c", script], env=_scrubbed_env(tmp_path), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    if writer == "systemd":
        text = (tmp_path / ".config/systemd/user/test.service").read_text()
        encoded = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
        assert f'Environment="{EXTRAS}={encoded}"' in text
        assert LEGACY + "=" not in text
    elif writer == "manifest":
        env = json.loads((tmp_path / "manifest.json").read_text())["env"]
        assert env[EXTRAS] == value
        assert LEGACY not in env
    else:
        path = tmp_path / ("result.plist" if writer == "agentctl" else "Library/LaunchAgents/test.plist")
        env = plistlib.loads(path.read_bytes())["EnvironmentVariables"]
        assert env[EXTRAS] == value
        assert LEGACY not in env


@pytest.mark.parametrize("live", [None, "", "/explicit"])
def test_agentctl_loading_does_not_synthesize_extras_before_update(tmp_path, live):
    path = _protection_file(tmp_path, {LEGACY: "/installed"})
    text = (ROOT / "dashboard/agentctl.sh").read_text()
    text = text[:text.index('\ncase "${1:-status}" in')]
    locate = 'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
    text = text.replace(locate, f"HERE={shlex.quote(str(ROOT / 'dashboard'))}")
    # Model a wrapper that loads the controller before exec'ing the installer.
    text += '\nprintf "%s\\n%s\\n" "${AGENTSTACK_EXTRA_PROTECTED_ROOTS+x}" "${AGENTSTACK_EXTRA_PROTECTED_ROOTS-}"\n'
    extra = {"AGENTSTACK_ENV_FILE": str(path)}
    if live is not None:
        extra[EXTRAS] = live
    result = subprocess.run([BASH, "-c", text], env=_scrubbed_env(tmp_path, **extra), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == (["", ""] if live is None else ["x", live])


def test_protection_guidance_precedes_mail_trailer_in_normal_and_auto_paths():
    text = INSTALLER.read_text()
    main = text[text.index("main() {"):text.index("# A Mail older than this checkout")]
    assert main.index("protection_restart_guidance") < main.index("install_payload")
    assert main.rindex("protection_restart_guidance") < main.index('case "$MAIL_UPDATE_RESULT" in')
    auto = main[main.index('if [[ "$MAIL_UPDATE_MODE" == auto ]]'):main.index("        return 0")]
    assert auto.rstrip().endswith("print_mail_result")
    assert main.rstrip().endswith("print_mail_result\n}")


def test_generated_unrelated_multiline_setting_does_not_confuse_protection_parser(tmp_path):
    path = _protection_file(tmp_path, {"AGENTSTACK_CLAUDE_MODELS": "model-one\nmodel-two", LEGACY: "/vault"})
    result = _protection_probe(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\0")[0] == "/vault"


@pytest.mark.parametrize("before,after,warn", [("/a:/b", "/b:/a", True), ("/a", "", True), ("/a", "/a", False)])
def test_extras_only_changes_get_coordinated_restart_guidance(tmp_path, before, after, warn):
    _protection_file(tmp_path, {EXTRAS: before})
    result = _dry_run(tmp_path, **{EXTRAS: after, "AGENTSTACK_PYTHON": sys.executable})
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("restart affected coordinated sessions together" in result.stderr) is warn


def test_background_services_drop_legacy_roots_and_export_only_extras(tmp_path):
    text = (ROOT / "dashboard/agentctl.sh").read_text()
    function = text[text.index("export_background_env() {"):text.index("start_background() {")]
    script = function + "\nEXTRA_PROTECTED_ROOTS='/extra'\nexport AGENTSTACK_PROTECTED_ROOTS='/stale'\nexport_background_env\n"
    script += 'printf "%s\\n%s\\n" "$AGENTSTACK_EXTRA_PROTECTED_ROOTS" "${AGENTSTACK_PROTECTED_ROOTS-unset}"\n'
    result = subprocess.run([BASH, "-e", "-c", script], env=_scrubbed_env(tmp_path), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["/extra", "unset"]
    installer = _installer_functions("start_supervised_background")
    assert installer.index('unset AGENTSTACK_PROTECTED_ROOTS') < installer.index('nohup "$PYTHON_BIN"')
