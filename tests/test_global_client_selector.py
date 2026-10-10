"""One normal selector and one missing-helper admission, preserving stdin."""

import os
from pathlib import Path
import runpy
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
ZSH = shutil.which("zsh")
SHELLS = ["/bin/bash", pytest.param(ZSH or "zsh", marks=pytest.mark.skipif(
    ZSH is None, reason="zsh is not installed"))]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("arguments", [[], ["--context"], ["--context=/missing"]])
def test_partial_install_admission_is_shared(tmp_path, arguments, shell):
    lib = tmp_path / "bin/lib"
    lib.mkdir(parents=True)
    loader = lib / "global-client-loader.sh"
    loader.write_bytes((ROOT / "bin/lib/global-client-loader.sh").read_bytes())
    env = {k: v for k, v in os.environ.items() if k != "AGENTSTACK_CLIENT_CONFIG"}
    value = subprocess.run(
        [shell, "-c", 'source "$1"; shift; ags_client_select "$@"', "selector", str(loader), *arguments],
        env=env,
        input="untouched hook payload",
        text=True,
        capture_output=True,
    )
    assert value.returncode == (2 if arguments else 125)
    assert ("GLOBAL_ENTRY_UNAVAILABLE" in value.stderr) == bool(arguments)
    assert not value.stdout
    (tmp_path / "runtime-client.json").symlink_to(tmp_path / "missing")
    value = subprocess.run(
        [shell, "-c", 'source "$1"; ags_client_select', "selector", str(loader)], env=env, text=True, capture_output=True
    )
    assert value.returncode == 2 and "GLOBAL_ENTRY_UNAVAILABLE" in value.stderr


def test_normal_selector_resolves_cli_env_and_implicit_once(tmp_path, monkeypatch):
    api = runpy.run_path(str(ROOT / "bin/lib/runtime_client.py"))
    selected = api["selected"]
    monkeypatch.setitem(selected.__globals__, "ROOT", tmp_path)
    monkeypatch.delenv("AGENTSTACK_CLIENT_CONFIG", raising=False)
    assert selected(["--old-legacy-flag"]) is None
    context = tmp_path / "runtime-client.json"
    context.symlink_to(tmp_path / "missing")
    assert selected([]) == context
    explicit = tmp_path / "explicit.json"
    explicit.write_text("{}")
    monkeypatch.setenv("AGENTSTACK_CLIENT_CONFIG", str(explicit))
    assert selected([]) == explicit
    assert selected(["--context", str(context)]) == context
    assert selected(["--context=" + str(context)]) == context
    with pytest.raises(api["ClientError"], match="CONTEXT_UNAVAILABLE"):
        selected(["--context"])


def test_loader_propagates_resolved_cli_context_to_existing_front_doors(tmp_path):
    context = tmp_path / "explicit.json"
    context.write_text("{}")
    loader = ROOT / "bin/lib/global-client-loader.sh"
    value = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; ags_client_select --context "$2" || exit $?; printf "%s" "$AGENTSTACK_CLIENT_CONFIG"',
            "selector",
            str(loader),
            str(context),
        ],
        env={k: v for k, v in os.environ.items() if k != "AGENTSTACK_CLIENT_CONFIG"},
        text=True,
        capture_output=True,
    )
    assert value.returncode == 0, value.stderr
    assert value.stdout == str(context)


def test_old_register_front_door_cannot_return_legacy_after_global_selection(tmp_path):
    lib = tmp_path / "bin/lib"
    lib.mkdir(parents=True)
    for name in ["global-client-loader.sh", "runtime_client.py"]:
        (lib / name).write_bytes((ROOT / "bin/lib" / name).read_bytes())
    (lib / "agentstack-register.sh").write_text("ags_global_entry() { return 125; }\n")
    context = tmp_path / "explicit.json"
    context.write_text("{}")
    value = subprocess.run(
        [
            "/bin/bash",
            str(lib / "global-client-loader.sh"),
            "spawn-child",
            "--context",
            str(context),
        ],
        env={k: v for k, v in os.environ.items() if k != "AGENTSTACK_CLIENT_CONFIG"},
        text=True,
        capture_output=True,
    )
    assert value.returncode == 2, value.stderr
    assert "GLOBAL_ENTRY_UNAVAILABLE" in value.stderr
    assert not value.stdout


@pytest.mark.parametrize("context_mode", ["absent", "explicit", "implicit", "broken"])
def test_old_runtime_without_select_preserves_legacy_preregister(tmp_path, context_mode):
    from test_preregister_child_naming import _FAKE_LIB

    lib = tmp_path / "bin/lib"
    lib.mkdir(parents=True)
    (lib / "global-client-loader.sh").write_bytes(
        (ROOT / "bin/lib/global-client-loader.sh").read_bytes())
    # Older runtime helpers have argparse operations but no select operation.
    (lib / "runtime_client.py").write_text(
        "import argparse\np=argparse.ArgumentParser()\n"
        "p.add_argument('operation', choices=['mode'])\np.parse_args()\n")
    (lib / "agentstack-register.sh").write_text(_FAKE_LIB)
    wrapper = tmp_path / "bin/agentstack-preregister-child"
    wrapper.write_bytes((ROOT / "bin/agentstack-preregister-child").read_bytes())
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENTSTACK_", "AGS_"))
           and k not in {"TMUX", "TMUX_PANE", "PROJECT_KEY"}}
    env.update(HOME=str(tmp_path), AGENTSTACK_HOME=str(tmp_path),
               AGENTSTACK_REGISTER_LIB=str(lib / "agentstack-register.sh"),
               AGENTSTACK_PROJECT_KEY="/synthetic", AGENTSTACK_ENV_FILE="")
    context = tmp_path / "runtime-client.json"
    if context_mode == "explicit":
        env["AGENTSTACK_CLIENT_CONFIG"] = str(tmp_path / "missing.json")
    elif context_mode == "implicit":
        context.write_text("{}")
    elif context_mode == "broken":
        context.symlink_to(tmp_path / "missing.json")
    token = tmp_path / "token"
    result = subprocess.run(
        ["/bin/bash", str(wrapper), "--name", "Curious-Curie", "--program", "codex",
         "--model", "fixture", "--token-file-out", str(token)],
        env=env, text=True, capture_output=True, timeout=10)
    if context_mode == "absent":
        assert result.returncode == 0, result.stderr
        assert result.stdout == "Curious-Curie\n" and token.is_file()
    else:
        assert result.returncode == 2, result.stderr
        assert "GLOBAL_ENTRY_UNAVAILABLE" in result.stderr
        assert not result.stdout and not token.exists()


@pytest.mark.parametrize("entry", ["pre-edit", "post-edit", "end-session", "session-start", "registered", "record-self"])
def test_absent_context_keeps_legacy_entry_even_when_disabled(tmp_path, entry):
    lib = tmp_path / "bin/lib"
    lib.mkdir(parents=True)
    loader = lib / "global-client-loader.sh"
    loader.write_bytes((ROOT / "bin/lib/global-client-loader.sh").read_bytes())
    (lib / "runtime_client.py").write_text("raise AssertionError('must not run')\n")
    (lib / "agentstack-register.sh").write_text("ags_global_entry() { return 0; }\n")
    env = {k: v for k, v in os.environ.items() if k != "AGENTSTACK_CLIENT_CONFIG"}
    env["AGENTSTACK_MAIL_DISABLED"] = "1"
    result = subprocess.run(["/bin/bash", str(loader), entry], env=env,
                            text=True, capture_output=True)
    assert result.returncode == 125
    assert not result.stdout and not result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_absent_context_does_not_start_python(tmp_path, shell):
    lib = tmp_path / "bin/lib"
    lib.mkdir(parents=True)
    loader = lib / "global-client-loader.sh"
    loader.write_bytes((ROOT / "bin/lib/global-client-loader.sh").read_bytes())
    for name in ("runtime_client.py", "agentstack-register.sh"):
        (lib / name).write_text("unused")
    python = tmp_path / "bin/python3"
    python.write_text("#!/bin/bash\necho unexpected-python >&2\nexit 99\n")
    python.chmod(0o700)
    env = {k: v for k, v in os.environ.items() if k != "AGENTSTACK_CLIENT_CONFIG"}
    env["PATH"] = str(tmp_path / "bin") + os.pathsep + env["PATH"]
    result = subprocess.run([shell, "-c", 'source "$1"; ags_client_select', "selector", str(loader)], env=env,
                            input="preserved stdin", text=True, capture_output=True)
    assert result.returncode == 125
    assert not result.stdout and not result.stderr
