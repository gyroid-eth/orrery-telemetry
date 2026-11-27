"""My Browser tests use only synthetic processes and disposable profiles."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hooks import child_tools, personal_browser as browser


def candidate(name, *, port=None, directory=None, state="ready", standard=True):
    return {"browser": name, "status": state, "port": port, "directory": directory, "standard": standard}


@pytest.mark.parametrize(("default", "expected"), [
    ("brave", "brave"), ("edge", "edge"), ("firefox", "chrome"), (None, "chrome"),
])
def test_default_is_preferred_only_from_ready_browsers(default, expected):
    rows = [
        candidate("brave", directory=r"C:\Brave User Data"),
        candidate("edge", port=9291),
        candidate("chrome", port=9201),
    ]
    assert browser._choose(rows, default, windows=True).browser == expected
    rows[0]["status"] = "debugging"
    if default == "brave":
        assert browser._choose(rows, default, windows=True).browser == "chrome"


def test_ready_custom_profile_keeps_spaces_and_uses_marker_autoconnect():
    directory = r"D:\ブラウザ data\Edge"
    rows = [candidate("edge", directory=directory, standard=False),
            candidate("edge", directory="ambiguous-relative", standard=True)]
    result = browser._choose(rows, "edge", windows=True)
    assert result.args == ("--autoConnect", "--userDataDir", directory)
    assert result.reason(True) == ""
    assert result.label == "Microsoft Edge"


@pytest.mark.parametrize(("browser_id", "label", "scheme"), [
    ("chrome", "Google Chrome", "chrome"),
    ("brave", "Brave", "brave"),
    ("edge", "Microsoft Edge", "edge"),
])
def test_detected_browser_missing_debugging_gets_its_own_instructions(browser_id, label, scheme):
    result = browser._choose([candidate(browser_id, state="debugging")])
    assert result.label == label
    assert result.state == "debugging"
    assert result.reason(True) == f"Open {scheme}://inspect/#remote-debugging in {label} and enable remote debugging."
    assert browser._choose([]).reason(False).startswith("Start Chrome, Brave, or Edge")


def test_macos_checks_marker_and_listener_owner_before_reporting_ready(tmp_path, monkeypatch):
    profile = tmp_path / "日本語 Brave data"
    profile.mkdir()
    marker = profile / 'DevToolsActivePort'
    marker.write_text("9299\n/devtools/browser/fixture\n")
    monkeypatch.setattr(browser, "_mac_processes", lambda: [
        (17, "brave", ["--user-data-dir", str(profile), "https://example.invalid"]),
        (21, "edge", []),
    ])
    monkeypatch.setattr(browser, "_mac_default", lambda: "brave")
    monkeypatch.setattr(browser, "_mac_owns_port", lambda pid, port: (pid, port) == (17, 9299))

    result = browser._mac_result()
    assert result.args == ('--autoConnect', '--userDataDir', str(profile))
    marker.write_text("9299\n/devtools/page/wrong-target\n")
    assert browser._mac_result().state == "debugging"
    marker.write_text("9298\n/devtools/browser/stale\n")
    assert browser._mac_result().state == "debugging"


def test_macos_fixed_port_uses_browser_url_only_for_process_owned_cdp(monkeypatch):
    monkeypatch.setattr(browser, "_mac_processes", lambda: [
        (21, "edge", ["--remote-debugging-port=9241", "https://example.invalid"]),
    ])
    monkeypatch.setattr(browser, "_mac_owns_port", lambda pid, port: (pid, port) == (21, 9241))
    monkeypatch.setattr(browser, "_is_fixed_cdp_port", lambda port: port == 9241)
    assert browser._mac_result().args == ("--browserUrl", "http://127.0.0.1:9241")
    monkeypatch.setattr(browser, "_is_fixed_cdp_port", lambda port: False)
    assert browser._mac_result().state == "debugging"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS kernel argv layout")
def test_macos_native_argv_preserves_paths_with_spaces():
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys;print('ready',flush=True);sys.stdin.read()",
         "--user-data-dir", "/folder with spaces/日本語", "--remote-debugging-port=9239"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline() == "ready\n"
        args = browser._mac_argv(child.pid)
        assert browser._mac_switch(args, "user-data-dir") == "/folder with spaces/日本語"
        assert browser._mac_switch(args, "remote-debugging-port") == "9239"
    finally:
        child.communicate(timeout=5)


def test_wsl_uses_windows_profile_and_local_runtime(monkeypatch, caplog):
    interop = {"WSL_INTEROP": "/fixture/interop"}
    monkeypatch.setattr(browser.runpy, "run_path", lambda _: {
        "_powershell": lambda: "/fixture/powershell.exe", "interop_env": lambda: interop
    })

    def run(argv, **options):
        assert argv[:4] == ["/fixture/powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
        assert options["env"] == interop and options["check"]
        assert all(name in argv[-1] for name in ("brave.exe", "msedge.exe", "chrome.exe"))
        data = {"default": "BraveHTML.123", "candidates": [
            candidate("chrome", port=9222),
            candidate("brave", directory=r"C:\Users\Perf User\Brave"),
        ]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(data))

    with monkeypatch.context() as ctx:
        ctx.setattr(browser.subprocess, "run", run)
        result = browser.discover(wsl=True)
    assert result.label == "Brave"
    monkeypatch.setattr(child_tools.sys, "platform", "linux")
    monkeypatch.setattr(child_tools, "running_in_wsl", lambda: True)
    monkeypatch.setattr(child_tools, "personal_browser", lambda wsl: result if wsl else pytest.fail("Windows expected"))
    monkeypatch.setattr(child_tools, "_windows_browser_npx", lambda: {
        "command": "/windows/node.exe", "args": [r"C:\npm\npx-cli.js"]
    })
    name, definition = child_tools._browser_mcp("personal")
    assert name == "chrome-devtools" and definition["command"] == "/windows/node.exe"
    assert definition["args"] == [
        r"C:\npm\npx-cli.js", "-y", "chrome-devtools-mcp@latest",
        "--autoConnect", "--userDataDir", r"C:\Users\Perf User\Brave", "--no-usage-statistics",
    ]

    def timeout(argv, **options):
        raise subprocess.TimeoutExpired(argv, options["timeout"])

    monkeypatch.setattr(browser.subprocess, "run", timeout)
    assert browser.discover(wsl=True).state == "interop"
    assert "powershell_timeout" in caplog.text


def test_catalog_uses_brand_only_for_managed_mcp(monkeypatch):
    result = browser.Result("brave", "debugging")
    monkeypatch.setattr(child_tools, "personal_browser", lambda wsl: result)
    modes = {key: ['off', 'personal'] for key in ('claude-code', 'codex', 'dsh', 'hermes', 'antigravity')}
    status = child_tools.personal_browser_catalog(modes, {}, wsl=True)
    assert set(status) == {'codex', 'dsh', 'hermes', 'antigravity'}
    assert status['codex']['browser'] == 'Brave' and status['codex']['unavailable']
    explicit = {"mcp_servers": {"chrome-devtools": {
        "command": "npx", "args": ["-y", "chrome-devtools-mcp", "--autoConnect", "--channel=canary"],
    }}}
    assert set(child_tools.personal_browser_catalog(modes, explicit, wsl=True)) == {'antigravity'}
    assert child_tools._browser_mcp('personal', explicit)[1] == explicit['mcp_servers']['chrome-devtools']


def test_dashboard_only_update_accepts_an_earlier_installed_launcher(monkeypatch):
    from dashboard import server

    modes = {"codex": ["off", "personal"]}
    launcher = SimpleNamespace(native_codex_config=lambda **_: {}, launch_browser_modes=lambda _: modes)
    monkeypatch.setattr(server, "_child_tools_module", lambda: launcher)
    monkeypatch.setattr(server, "_child_resume_module", lambda: None)
    assert server._spawn_browser_catalog() == {"browser_modes": modes}
