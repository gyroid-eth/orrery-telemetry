"""One normal selector and one missing-helper admission, preserving stdin."""

import os
from pathlib import Path
import runpy
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("arguments", [[], ["--context"], ["--context=/missing"]])
def test_partial_install_admission_is_shared(tmp_path, arguments):
    lib = tmp_path / "bin/lib"
    lib.mkdir(parents=True)
    loader = lib / "global-client-loader.sh"
    loader.write_bytes((ROOT / "bin/lib/global-client-loader.sh").read_bytes())
    env = {k: v for k, v in os.environ.items() if k != "AGENTSTACK_CLIENT_CONFIG"}
    value = subprocess.run(
        ["/bin/bash", str(loader), "select", *arguments],
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
        ["/bin/bash", str(loader), "select"], env=env, text=True, capture_output=True
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
