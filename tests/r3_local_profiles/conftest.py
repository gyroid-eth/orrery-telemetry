"""Isolated fixtures and deliberately missing, proposed R3 product seams.

Keep imports inside tests: absent implementation must not prevent collection of
the instrument/negative-control tests. No importorskip, xfail, or fake resolver.
"""
import importlib
import hashlib
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace

import pytest

from .support import Canary, FIXTURE_ADDRESSES, MAIL, profile_text


@pytest.fixture(autouse=True)
def isolated_local_profile_home(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "bin" / "lib"))
    for key in tuple(os.environ):
        if key.startswith(("AGENTSTACK_", "ANTHROPIC_", "OPENAI_", "CLAUDE_", "CODEX_", "TMUX", "AWS_", "GOOGLE_")) or key.upper().endswith("_PROXY"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    for name in ("tmp", "runtime", "cwd", "vault", "task-dir", "bin"):
        (tmp_path / name).mkdir(mode=0o700)
    for key, value in {"HOME": home, "TMPDIR": tmp_path / "tmp",
                       "AGENTSTACK_HOME": home / ".agentstack",
                       "AGENTSTACK_RUNTIME_DIR": tmp_path / "runtime",
                       "CODEX_HOME": home / ".codex",
                       "CLAUDE_CONFIG_DIR": home / ".claude"}.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + ":/usr/bin:/bin")
    # Only currently registered fixture sockets, not arbitrary loopback ports.
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    def check_address(address):
        if not isinstance(address, tuple) or address not in FIXTURE_ADDRESSES:
            raise AssertionError("fixture attempted non-fixture network access")
    def loopback_only(sock, address):
        check_address(address)
        return connect(sock, address)
    def loopback_only_ex(sock, address):
        check_address(address)
        return connect_ex(sock, address)
    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    monkeypatch.setattr(socket.socket, "connect_ex", loopback_only_ex)


@pytest.fixture
def canary():
    return Canary()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "home" / ".agentstack" / "local-models"
    root.mkdir(parents=True, mode=0o700)
    root.parent.chmod(0o700)
    profile = root / "fixture.toml"
    profile.write_text(profile_text())
    profile.chmod(0o600)
    task = tmp_path / "task-dir" / "task.md"
    task.write_text("Read task, reserve, edit Scratch, renew, release, report.\n")
    handoff = tmp_path / "vault" / "handoff.md"
    handoff.write_text("Approved handoff.\n")
    scratch = tmp_path / "cwd" / "Scratch.md"
    scratch.write_text("before\n")
    settings = tmp_path / "home" / "settings.json"
    settings.write_text('{"fixture_sentinel":"keep","permissions":{"deny":["fixture-deny"]}}')
    runner = tmp_path / "home" / ".agentstack" / "fixture-auth-runner.py"
    runner.write_text("# Non-executable fixture resource, never the installed auth runner.\n")
    runner.chmod(0o500)
    dependency = tmp_path / "home" / ".agentstack" / "fixture-auth-dependency.txt"
    dependency.write_text("fixture resource version 1\n")
    dependency.chmod(0o600)
    runner_plan = {"path": str(runner), "sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
                   "contract_version": 1, "dependency_digests": {
                       str(dependency): hashlib.sha256(dependency.read_bytes()).hexdigest()}}
    scope = {"read": [str(tmp_path / "cwd"), str(task), str(handoff)],
             "edit": [str(scratch)], "mail_tools": list(MAIL),
             "bash": ["cd approved-cwd && fixture-echo", "fixture-echo | fixture-filter"],
             "built_in_read": [str(tmp_path / "cwd")],
             "sandbox_auto_allow": False, "inherited_bash_allow": [],
             "reservation_hook": "enabled", "settings_sources": [str(settings)],
             "cli_version": "fixture-pinned"}
    context = {"local_models_root": str(root), "operator_uid": os.getuid(),
               "runtime": str(tmp_path / "runtime"), "mode": "global",
               "effective_scope": scope, "auth_runner": runner_plan,
               "approved_settings_sha256": hashlib.sha256(settings.read_bytes()).hexdigest(),
               "auth_context": {"cwd": str(root), "env": {
                   "HOME": str(tmp_path / "home"), "USER": "fixture", "LOGNAME": "fixture",
                   "PATH": "/usr/bin:/bin", "LANG": "C"}, "timeout_ms": 5000,
                   "stdout_limit_bytes": 8192, "refresh_interval_ms": 300000},
               "network": "fixture-loopback-only", "effects": []}
    caller = {"provider": "codex", "model": "local:fixture", "cwd": str(tmp_path / "cwd"),
              "task": str(task), "name": "FixtureChild", "parent": "FixtureParent",
              "tools": {}, "base": "mail-only", "clear_tools": False,
              "detached": True, "resources": [str(scratch)], "worktree": False,
              "chrome": False}
    return SimpleNamespace(root=root, profile=profile, context=context, caller=caller,
                           task=task, handoff=handoff, scratch=scratch, settings=settings,
                           scope=scope, tmp=tmp_path, runner=runner, dependency=dependency)


def product_module(name):
    # Proposed private seams, not a second implementation in tests. The R3
    # implementer may change these bindings together with the one-page map.
    return importlib.import_module(name)


@pytest.fixture
def profiles():
    return product_module("local_model_profiles")


@pytest.fixture
def launch_intent():
    return product_module("runtime_launch_intent")


@pytest.fixture
def auth_runner():
    return product_module("local_model_auth")


@pytest.fixture
def native_probe():
    # This is a TEST adapter seam, not a production API or replacement CLI.
    # It must eventually bind real pinned CLI runs to the existing launch,
    # receipt, native permissions and fake Mail transports. Until supplied,
    # these tests are red, not evidence about native authentication/permissions.
    return importlib.import_module(".native_acceptance", package=__package__).probe


@pytest.fixture
def caller_probe(workspace, monkeypatch, request):
    # Only caller delivery gets a short REAL root (macOS control.sock limit).
    # No symlink aliases, modified path guards or native38 bootstrap are used.
    from .caller_acceptance import DeliveryCallerProbe
    base = '/private/tmp' if Path('/private/tmp').is_dir() else None
    with tempfile.TemporaryDirectory(prefix='r3-caller-', dir=base) as raw:
        root = Path(raw).resolve(strict=True)
        root.chmod(0o700)
        for name in ('home', 'tmp', 'runtime', 'cwd', 'vault', 'task-dir', 'bin'):
            (root / name).mkdir(mode=0o700)
        rebuilt = globals()['workspace'].__wrapped__(root)
        workspace.__dict__.clear()
        workspace.__dict__.update(rebuilt.__dict__)
        for key, value in {'HOME':root/'home', 'TMPDIR':root/'tmp',
                          'AGENTSTACK_HOME':root/'home/.agentstack',
                          'AGENTSTACK_RUNTIME_DIR':root/'runtime',
                          'CODEX_HOME':root/'home/.codex',
                          'CLAUDE_CONFIG_DIR':root/'home/.claude'}.items():
            monkeypatch.setenv(key, str(value))
        enabled = getattr(request, 'param', True)
        assert type(enabled) is bool
        yield DeliveryCallerProbe(workspace, enabled=enabled)
