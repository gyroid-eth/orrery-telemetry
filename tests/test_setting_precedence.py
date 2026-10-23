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

import json
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
        "PROTECTED_ROOTS": str(project),
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
    env["PATH"] = f"{_fake_bin(pathlib.Path(tempfile.mkdtemp(dir=home.parent)))}:{env['PATH']}"
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
    # Keep the literal parser on the same Python as pytest, with no Codex CLI.
    result = subprocess.run(
        ["bash", "-c", probe, "install.sh", *args],
        env=_scrubbed_env(
            home, PATH=str(pathlib.Path(sys.executable).parent) + ":/usr/bin:/bin", **(env or {})
        ),
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


def test_p2_2_unlisted_nondefault_values_are_preserved(tmp_path):
    """#354: missing choice names no longer discard saved nondefault values."""
    home, _ = _previous_install(
        tmp_path,
        not_chosen=("PATH_VALUE", "PYTHON_BIN", "DASHBOARD_LOG_MAX_BYTES"),
        PATH_VALUE="/an/older/default/bin:/usr/bin:/bin",
        DASHBOARD_LOG_MAX_BYTES="1048576",
    )
    assert _resolve_like_the_installer(
        home, "PATH_VALUE", "PYTHON_INHERITED", "DASHBOARD_LOG_MAX_BYTES", "PORT"
    ) == [
        "/an/older/default/bin:/usr/bin:/bin",
        sys.executable,
        "1048576",
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
        # The preservation rule also covers nondefault PATH and Python.
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/codex/bin",
        sys.executable,
        "AGENTSTACK_PORT AGENTSTACK_PATH AGENTSTACK_PYTHON",
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
    ) == [chosen["PATH_VALUE"], "19876"]
    assert _chosen(home, env=echoed) == set(chosen["CHOSEN_SETTINGS"].split()) | {"AGENTSTACK_PATH"}
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
        'echo "roots=$AGENTSTACK_PROTECTED_ROOTS"\n',
    )
    explicit = f"{home}/Developer/orrery-telemetry"
    assert lines == [f"before={explicit}", f"after={explicit}", f"roots={explicit}"]


def test_without_an_explicit_project_key_env_sh_supplies_it(tmp_path):
    home = _home_with_installed_env(tmp_path)
    lines = _load_env(
        home,
        LAUNCH_LIB,
        "ags_load_env\n"
        'echo "$AGENTSTACK_PROJECT_KEY"\n'
        'echo "$AGENTSTACK_PROTECTED_ROOTS"\n'
        'echo "$AGENTSTACK_CODEX_BIN"\n',
    )
    assert lines == ["/installed/StudyPlanner", "/installed/StudyPlanner", "/installed/codex"]


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


# Issue354: preserve existing nondefault values even with an incomplete record.
def _issue354_old_env(tmp_path, monkeypatch, record):
    home, project = tmp_path / 'home', tmp_path / 'project'
    home.mkdir()
    project.mkdir()
    values = {
        'PROJECT_KEY': str(project), 'PROTECTED_ROOTS': str(project),
        'PORT': '19000', 'LABEL_PREFIX': 'org.agentstack.test.issue354',
        'AUTO_OPEN_CHILD_SETTING': '0', 'PORTRAITS_DIR_SETTING': '~/custom portraits',
        'NATIVE_MAIL_STATE_ROOT': str(tmp_path / 'mail-state'),
        'NATIVE_MAIL_SERVICE_ROOT': str(tmp_path / 'mail-service'),
        'NATIVE_MAIL_MANAGEMENT_SOCKET': str(tmp_path / 'mail.sock'),
        'CHOSEN_SETTINGS': ('AGENTSTACK_AUTO_OPEN_CHILD AGENTSTACK_PORTRAITS_DIR'
                            if record == 'selected' else 'AGENTSTACK_PORT' if record == 'partial' else ''),
    }
    with monkeypatch.context() as patch:
        patch.setitem(globals(), 'INSTALLER', pathlib.Path(os.environ.get('ISSUE354_OLD_INSTALLER', str(INSTALLER))))
        _write_env_sh_like_the_installer(home, values)
    if record == 'absent':
        # This is a pre-record/record-removed fixture, not a claim that the
        # reporter's 5918caa writer omitted its already present record.
        env_file = home / '.agentstack/env.sh'
        env_file.write_text(''.join(line for line in env_file.read_text().splitlines(keepends=True)
                                   if not line.startswith('export AGENTSTACK_CHOSEN_SETTINGS=')))
    return home, values


def _issue354_resolve_and_render(home, *, args=(), env=None):
    outputs = ('AUTO_OPEN_CHILD_SETTING', 'PORTRAITS_DIR_SETTING', 'CHOSEN_SETTINGS',
               'PROJECT_KEY', 'PROTECTED_ROOTS', 'PORT', 'LABEL_PREFIX',
               'NATIVE_MAIL_STATE_ROOT', 'NATIVE_MAIL_SERVICE_ROOT', 'NATIVE_MAIL_MANAGEMENT_SOCKET')
    resolved = _resolve_like_the_installer(home, *outputs, args=args, env=env)
    _write_env_sh_like_the_installer(home, dict(zip(outputs, resolved)))
    return dict(zip(outputs, resolved))


@pytest.mark.parametrize('record', ['absent', 'selected', 'empty', 'partial'])
@pytest.mark.parametrize('echo', [False, True])
def test_issue354_actual_old_writer_resolution_and_second_update(tmp_path, monkeypatch, record, echo):
    home, original = _issue354_old_env(tmp_path, monkeypatch, record)
    expected = ['0', '~/custom portraits']
    observations = []
    for number in range(2):
        echoed = {}
        if echo:
            for line in (home / '.agentstack/env.sh').read_text().splitlines():
                if line.startswith('export '):
                    name, _, raw = line[len('export '):].partition('=')
                    if name in ('AGENTSTACK_AUTO_OPEN_CHILD', 'AGENTSTACK_PORTRAITS_DIR'):
                        echoed[name] = (shlex.split(raw) or [''])[0]
        resolved = _issue354_resolve_and_render(home, env=echoed)
        values = [resolved['AUTO_OPEN_CHILD_SETTING'], resolved['PORTRAITS_DIR_SETTING']]
        observations.append({'update': number + 1, 'values': values,
                             'two_names_chosen': [name in resolved['CHOSEN_SETTINGS'].split() for name in
                                ('AGENTSTACK_AUTO_OPEN_CHILD', 'AGENTSTACK_PORTRAITS_DIR')]})
        print('ISSUE354_POLICY_STEP=' + json.dumps(dict(record=record, echo=echo, **observations[-1])))
        assert values == expected
        assert observations[-1]['two_names_chosen'] == [True, True]
        for name in ('PROJECT_KEY', 'PROTECTED_ROOTS', 'LABEL_PREFIX', 'NATIVE_MAIL_STATE_ROOT',
                     'NATIVE_MAIL_SERVICE_ROOT', 'NATIVE_MAIL_MANAGEMENT_SOCKET'):
            assert resolved[name] == original[name]
        written = (home / '.agentstack/env.sh').read_text()
        for name, expected_value in zip(('AGENTSTACK_AUTO_OPEN_CHILD', 'AGENTSTACK_PORTRAITS_DIR'), values):
            line = next(line for line in written.splitlines() if line.startswith('export ' + name + '='))
            assert (shlex.split(line.partition('=')[2]) or [''])[0] == expected_value
    print('ISSUE354_MATRIX=' + json.dumps(dict(record=record, echo=echo, observations=observations)))


@pytest.mark.parametrize('choice', ['empty-reset', 'reset-settings', 'explicit-custom'])
def test_issue354_explicit_request_and_rendered_followup(tmp_path, monkeypatch, choice):
    home, original = _issue354_old_env(tmp_path, monkeypatch, 'selected')
    args, env = (), {}
    if choice == 'empty-reset':
        env = {'AGENTSTACK_AUTO_OPEN_CHILD': '', 'AGENTSTACK_PORTRAITS_DIR': ''}
    elif choice == 'reset-settings':
        args = ('--reset-settings',)
    else:
        # First render defaults with no choice, then explicitly choose 0 and
        # a custom folder, matching the reported environment-override recovery.
        _issue354_resolve_and_render(home, args=('--reset-settings',))
        env = {'AGENTSTACK_AUTO_OPEN_CHILD': '0', 'AGENTSTACK_PORTRAITS_DIR': '~/explicit portraits'}
    first = _issue354_resolve_and_render(home, args=args, env=env)
    second = _issue354_resolve_and_render(home)
    expected = ['0', '~/explicit portraits'] if choice == 'explicit-custom' else ['1', '']
    for resolved in (first, second):
        assert [resolved['AUTO_OPEN_CHILD_SETTING'], resolved['PORTRAITS_DIR_SETTING']] == expected
        for name in ('PROJECT_KEY', 'PROTECTED_ROOTS', 'LABEL_PREFIX', 'NATIVE_MAIL_STATE_ROOT',
                     'NATIVE_MAIL_SERVICE_ROOT', 'NATIVE_MAIL_MANAGEMENT_SOCKET'):
            assert resolved[name] == original[name]
    print('ISSUE354_REQUEST=' + json.dumps(dict(choice=choice, first=expected, second=expected)))


@pytest.mark.parametrize('removed', ['AGENTSTACK_AUTO_OPEN_CHILD', 'AGENTSTACK_PORTRAITS_DIR'])
def test_issue354_removing_the_actual_value_resets_only_that_setting(tmp_path, monkeypatch, removed):
    home, _ = _issue354_old_env(tmp_path, monkeypatch, 'selected')
    env_file = home / '.agentstack/env.sh'
    env_file.write_text(''.join(
        line for line in env_file.read_text().splitlines(keepends=True)
        if not line.startswith('export ' + removed + '=')
    ))
    expected = ['1', '~/custom portraits'] if removed == 'AGENTSTACK_AUTO_OPEN_CHILD' else ['0', '']
    for _ in range(2):
        resolved = _issue354_resolve_and_render(home)
        assert [resolved['AUTO_OPEN_CHILD_SETTING'], resolved['PORTRAITS_DIR_SETTING']] == expected
        assert removed not in resolved['CHOSEN_SETTINGS'].split()


def _issue354_plan(home, *, args=(), env=None):
    """Run real resolution and its real plan formatter, before any OS probes."""
    text = INSTALLER.read_text(encoding='utf-8')
    locate = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
    head = text[:text.index('PREFLIGHT_OS=""')].replace(
        locate, f'SCRIPT_DIR={shlex.quote(str(ROOT / "scripts"))}'
    )
    function = re.search(r'^report_setting_changes\(\) \{\n.*?^\}', text, re.M | re.S)
    assert function is not None
    script = head + '\nsay() { printf "%s\\n" "$*"; }\n' + function[0] + '\nreport_setting_changes\n'
    result = subprocess.run(
        [BASH, '-c', script, 'install.sh', *args],
        env=_scrubbed_env(home, PATH=str(pathlib.Path(sys.executable).parent) + ':/usr/bin:/bin', **(env or {})),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


@pytest.mark.parametrize('scenario', ['preserve', 'reset', 'override'])
def test_issue354_plan_matches_the_values_actually_written(tmp_path, monkeypatch, scenario):
    home, _ = _issue354_old_env(tmp_path, monkeypatch, 'partial')
    args = ('--reset-settings',) if scenario == 'reset' else ()
    env = {'AGENTSTACK_AUTO_OPEN_CHILD': '1', 'AGENTSTACK_PORTRAITS_DIR': '~/new portraits'} if scenario == 'override' else {}
    # An unrelated setting must never be dumped by the new change report.
    env_file = home / '.agentstack/env.sh'
    with env_file.open('a') as stream:
        stream.write("export AGENTSTACK_UNRELATED_PRIVATE_VALUE='do-not-print-this'\n")
    plan = _issue354_plan(home, args=args, env=env)
    resolved = _issue354_resolve_and_render(home, args=args, env=env)
    values = [resolved['AUTO_OPEN_CHILD_SETTING'], resolved['PORTRAITS_DIR_SETTING']]
    if scenario == 'preserve':
        assert values == ['0', '~/custom portraits']
        assert plan == []
    else:
        assert values == (['1', ''] if scenario == 'reset' else ['1', '~/new portraits'])
        assert plan == [
            'setting change: AGENTSTACK_AUTO_OPEN_CHILD: 0 -> ' + values[0],
            'setting change: AGENTSTACK_PORTRAITS_DIR: ~/custom portraits -> ' + (values[1] or '(empty)'),
        ]
    assert all('do-not-print-this' not in line for line in plan)
    main = INSTALLER.read_text().split('\nmain() {\n', 1)[1]
    assert main.index('\n  report_setting_changes\n') < main.index('\n  if ! run_preflight;') < main.index('\n  write_env_file\n')
    print('ISSUE354_PLAN=' + json.dumps(dict(scenario=scenario, plan=plan, applied=values)))
