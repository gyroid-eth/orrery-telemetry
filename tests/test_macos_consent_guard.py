"""Run each pre-execution adapter with synthetic payloads."""

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks/macos_consent_guard.py"
KEY_HOOK = ROOT / "hooks/deny-llm-key-read.py"
CODEX = ROOT / "integrations/codex_app/plugin/scripts/run-key-read-guard.sh"


def invoke(command, payload, cwd, tmux=True):
    env = {
        **os.environ,
        "AGENTSTACK_HOME": str(ROOT),
        "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test-macos-consent",
        "AGENTSTACK_PYTHON": sys.executable,
    }
    if tmux:
        env["TMUX"] = "consent-fixture,1,0"
    else:
        env.pop("TMUX", None)
    return subprocess.run(command, input=json.dumps(payload), text=True, cwd=cwd,
                          env=env, capture_output=True, check=False, timeout=8)


def shell_payload(cwd, command, name="Bash", key="command"):
    return {"cwd": str(cwd), "tool_name": name, "tool_input": {key: command}}


def agy_payload(cwd, command):
    return {"workspacePaths": [str(cwd)],
            "toolCall": {"name": "run_command", "args": {"CommandLine": command, "Cwd": str(cwd)}}}


MATRIX = [
    ("screencapture /tmp/fake.png", True, True),
    ("/usr/sbin/screencapture -x /tmp/fake.png", True, True),
    ("/usr/bin/osascript -e 'display dialog test'", True, True),
    ('python3 -c "import Quartz; Quartz.CGWindowListCreateImage(0,0,0,0)"', True, True),
    ("python3 - <<'PY'\nfrom Quartz import CGWindowListCreateImage as capture\ncapture(0,0,0,0)\nPY", True, True),
    ("python3 -c 'import Quartz\nQuartz.CGDisplayCreateImage(0)'", True, True),
    ('swift -e "import CoreGraphics; CGDisplayCreateImage(0)"', True, True),
    ("env MODE=test bash -lc '/usr/sbin/screencapture fixture.png'", True, True),
    ("printf '%s' `screencapture /tmp/fake.png`", True, True),
    ("grep screencapture README.md", False, True),
    ("command -v /usr/sbin/screencapture", False, True),
    ("printf '%s' '\"$(screencapture)\"'", False, True),
    ("cat <<'TXT'\n/usr/sbin/screencapture\nCGDisplayCreateImage(0)\nTXT", False, True),
    ("osascript -e 'display notification test'", False, False),
]


@pytest.mark.parametrize(("command", "denied", "tmux"), MATRIX)
def test_shell_hooks_and_antigravity(tmp_path, command, denied, tmux):
    denied = denied and sys.platform == "darwin"
    legacy = ROOT / "hooks/deny-llm-key-read.sh"
    for argv, payload in (
        ([sys.executable, str(HOOK)], shell_payload(tmp_path, command)),  # isolated Claude
        (["/bin/bash", str(legacy)], shell_payload(tmp_path, command)),   # standard Claude
        (["/bin/bash", str(CODEX)], shell_payload(tmp_path, command, "exec_command", "cmd")),
    ):
        result = invoke(argv, payload, tmp_path, tmux)
        assert result.returncode == (2 if denied else 0), result.stderr
        assert ("MACOS_CONSENT_DENIED:" in result.stderr) is denied

    result = invoke([sys.executable, str(HOOK), "--antigravity"], agy_payload(tmp_path, command),
                    tmp_path, tmux)
    assert result.returncode == 0, result.stderr
    verdict = json.loads(result.stdout)
    assert verdict["decision"] == ("deny" if denied else "allow")
    assert ("Cua Driver" in verdict.get("reason", "")) is denied


@pytest.mark.parametrize(("command", "denied", "tmux"), MATRIX[:4] + MATRIX[-5:])
def test_hermes_adapter(tmp_path, monkeypatch, command, denied, tmux):
    denied = denied and sys.platform == "darwin"
    monkeypatch.setenv("AGENTSTACK_HOME", str(ROOT))
    if tmux:
        monkeypatch.setenv("TMUX", "fixture,1,0")
    else:
        monkeypatch.delenv("TMUX", raising=False)
    before = runpy.run_path(str(ROOT / "integrations/hermes/thin_guard.py"))["before"]
    variables = before.__globals__
    common_hook = variables["call_hook"]
    monkeypatch.setitem(variables, "_cwd", lambda *_: str(tmp_path))
    monkeypatch.setitem(variables, "call_hook", lambda name, payload:
                        common_hook(name, payload) if name == KEY_HOOK.name else True)
    verdict = before("terminal", {"command": command})
    assert bool(verdict) is denied
    if denied:
        assert verdict["action"] == "block" and "Cua Driver" in verdict["message"]


def test_dsh_native_middleware_adapter(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    cases = MATRIX[:4] + MATRIX[-5:]
    code = """
    import {apply, callHook} from './integrations/dsh/orrery_tools.mjs';
    import assert from 'node:assert/strict';
    const cases = JSON.parse(process.argv[1]);
    const cwd = process.argv[2];
    for (const [command, expected, tmux] of cases) {
      const handlers = new Map();
      const env = {AGENTSTACK_PYTHON: process.argv[3], TMUX: tmux ? 'fixture,1,0' : ''};
      apply({on: (name, handler) => handlers.set(name, handler)},
            {cwd, env, hooks_dir: process.cwd() + '/hooks'},
            (file, payload, hookEnv) => file.endsWith('/deny-llm-key-read.py') ?
                callHook(file, payload, hookEnv) : {ok: true});
      let ran = false;
      const verdict = await handlers.get('tools/pre-execute')(
          {name: 'bash', arguments: {command}}, () => { ran = true; });
      const denied = process.platform === 'darwin' && expected;
      assert.equal(ran, !denied, command);
      if (denied) {
        assert.equal(verdict.kind, 'deny');
        assert.match(verdict.reason, /Cua Driver/);
      }
    }
    """
    result = subprocess.run([node, "--input-type=module", "--eval", code, json.dumps(cases),
                             str(tmp_path), sys.executable], cwd=ROOT, text=True, capture_output=True,
                            check=False, timeout=20)
    assert result.returncode == 0, result.stderr


def test_only_invoked_source_is_checked(tmp_path):
    (tmp_path / "fixture.py").write_text("import Quartz\nQuartz.CGDisplayCreateImage(0)\n")
    (tmp_path / "note.py").write_text("print('CGDisplayCreateImage(0)')\n")
    for command, expected in (("python3 fixture.py", 2), ("cat fixture.py", 0), ("python3 note.py", 0)):
        result = invoke([sys.executable, str(HOOK)], shell_payload(tmp_path, command), tmp_path)
        assert result.returncode == (expected if sys.platform == "darwin" else 0), result.stderr


@pytest.mark.parametrize(("platform", "provider"), [
    ("darwin", "claude"), ("darwin", "gemini"), ("linux", "claude"), ("linux", "gemini"),
])
def test_fixed_home_provider_hooks_follow_platform(tmp_path, monkeypatch, platform, provider):
    import shlex
    from types import SimpleNamespace

    api = runpy.run_path(str(ROOT / "bin/lib/runtime_delivery.py"))
    entry = api["RuntimeDelivery"].provider_command
    monkeypatch.setitem(entry.__globals__, "instruction_api",
                        lambda: {"provider_instructions": lambda *args: None, "Refused": ValueError})
    monkeypatch.setattr(sys, "platform", platform)
    generated = {}
    client = SimpleNamespace(runtime_dir=tmp_path, config={"wrapper_root": str(ROOT)},
                             path=tmp_path / "context.json")
    delivery = SimpleNamespace(
        client=client, directory=lambda path: path, child_tool_plan=lambda *args: ({}, [], ""),
        launch_task=lambda row: "",
        generated_config=lambda path, data, **options: generated.update({path.name: (data, options)}),
    )
    entry(delivery, {"provider": provider, "launch_kind": "startup", "launch_id": "fixture",
                     "cwd": str(tmp_path)})
    if provider == "gemini":
        if platform == "darwin":
            hooks, options = generated["hooks.json"]
            matcher = hooks["orrery-consent"]["PreToolUse"][0]
            assert matcher["matcher"] == "^run_command$"
            assert shlex.split(matcher["hooks"][0]["command"])[1:] == [str(HOOK), "--antigravity"]
            assert options == {"preserve_groups": True}
        else:
            assert "hooks.json" not in generated
    else:
        hooks = generated["orrery-hooks.json"][0]["hooks"]
        assert hooks["PreToolUse"][0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
        bash = [group for group in hooks["PreToolUse"] if group["matcher"] == "Bash"]
        if platform == "darwin":
            assert len(bash) == 1
            assert shlex.split(bash[0]["hooks"][0]["command"])[1:] == [str(HOOK)]
        else:
            assert not bash


def test_legacy_headless_antigravity_hook(tmp_path, monkeypatch):
    import shlex
    from hooks import gemini_interactive

    destination = tmp_path / ".agents" / "hooks.json"
    monkeypatch.setattr(sys, "platform", "linux")
    gemini_interactive.write_headless_consent_hook(tmp_path)
    assert not destination.exists()
    monkeypatch.setattr(sys, "platform", "darwin")
    gemini_interactive.write_headless_consent_hook(tmp_path)
    declaration = json.loads(destination.read_text())
    group = declaration["orrery-consent"]["PreToolUse"][0]
    assert group["matcher"] == "^run_command$"
    runner = shlex.split(group["hooks"][0]["command"])
    assert runner[1:] == [str(HOOK), "--antigravity"]
    result = invoke(runner, agy_payload(tmp_path, "osascript -e fixture"), tmp_path)
    # The actual interpreter, not the generator's platform fixture, gates execution.
    assert json.loads(result.stdout)["decision"] == ("deny" if os.uname().sysname == "Darwin" else "allow")
    with pytest.raises(ValueError, match="already has .agents/hooks.json"):
        gemini_interactive.write_headless_consent_hook(tmp_path)
    assert json.loads(destination.read_text()) == declaration
