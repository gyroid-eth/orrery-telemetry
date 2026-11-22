"""The dashboard keeps one tmux consumer per endpoint (2026-10-16).

_tmux_run built a new legacy consumer for every tmux command, and building one
resolves and verifies the endpoint with a display-message process: about a
fifth of a /api/agents computation in a profile. The consumer is now kept while
the endpoint's socket is the same file (same device and inode). Every command
is still admitted by the consumer; a restarted tmux server makes a new socket
file, so the next command builds a new consumer.

These run a real tmux server in a private TMUX_TMPDIR, with the installed
client library under a temporary AGENTSTACK_HOME.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from dashboard import server
from _installed_client_payload import copy_client_library

TMUX = shutil.which("tmux")
pytestmark = pytest.mark.skipif(TMUX is None, reason="tmux is not installed")


@pytest.fixture
def endpoint(monkeypatch):
    base = "/private/tmp" if Path("/private/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="ct-", dir=base) as directory:
        root = Path(directory).resolve()
        home = root / "home" / ".agentstack"
        (home / "hooks").mkdir(parents=True)
        copy_client_library(home)
        sockets = root / "s"
        sockets.mkdir(mode=0o700)
        for key in list(os.environ):
            if key.startswith("AGENTSTACK_") or key in ("TMUX", "TMUX_PANE"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("AGENTSTACK_HOME", str(home))
        monkeypatch.setenv("TMUX_TMPDIR", str(sockets))
        monkeypatch.setattr(server, "HOOKS_DIR", str(home / "hooks"))
        env = {**os.environ}

        def tmux(*args, check=True):
            return subprocess.run([TMUX, *args], env=env, capture_output=True, text=True, check=check)

        tmux("new-session", "-d", "-s", "Alpha", "sleep 600")
        built, admitted, entries = [], [], []
        real_build = server._build_legacy_tmux_consumer
        real_entry = server._tmux_helper_entry

        def counting_entry():
            entries.append(1)
            return real_entry()

        def counting_build(entry=None):
            consumer = real_build(entry)
            built.append(consumer)
            if consumer is not None:
                real_admission = consumer.admission

                def admission(target, operation):
                    admitted.append((target, operation))
                    return real_admission(target, operation)

                consumer.admission = admission
            return consumer

        monkeypatch.setattr(server, "_build_legacy_tmux_consumer", counting_build)
        monkeypatch.setattr(server, "_tmux_helper_entry", counting_entry)
        server._reset_tmux_consumer_cache()
        try:
            yield {"tmux": tmux, "built": built, "admitted": admitted, "entries": entries,
                   "sockets": sockets, "root": root, "home": home, "env": env}
        finally:
            tmux("kill-server", check=False)
            server._reset_tmux_consumer_cache()


def test_one_consumer_serves_many_commands_and_each_is_admitted(endpoint):
    for _ in range(5):
        assert server._tmux(["display-message", "-p", "-t", "=Alpha", "#{session_name}"]).strip() == "Alpha"
    assert len(endpoint["built"]) == 1
    assert endpoint["admitted"] == [("=Alpha", "capture")] * 5


def test_inventory_still_reads_through_the_kept_consumer(endpoint):
    for _ in range(3):
        assert "Alpha" in server._tmux(["list-sessions", "-F", "#{session_name}"])
    assert len(endpoint["built"]) == 1


def test_a_restarted_server_is_a_new_socket_and_a_new_consumer(endpoint):
    tmux = endpoint["tmux"]
    assert server._tmux(["display-message", "-p", "-t", "=Alpha", "#{session_name}"]).strip() == "Alpha"
    first = endpoint["built"][-1]
    identity = first.transport.identity
    tmux("kill-server")
    time.sleep(0.2)
    tmux("new-session", "-d", "-s", "Beta", "sleep 600")
    assert not server._consumer_still_current(first)          # the identity check fails
    assert server._tmux(["display-message", "-p", "-t", "=Beta", "#{session_name}"]).strip() == "Beta"
    assert len(endpoint["built"]) == 2
    assert endpoint["built"][-1].transport.identity != identity
    # admission continues on the new consumer, for every command
    before = len(endpoint["admitted"])
    for _ in range(3):
        server._tmux(["display-message", "-p", "-t", "=Beta", "#{session_name}"])
    assert len(endpoint["admitted"]) == before + 3
    assert len(endpoint["built"]) == 2


def test_a_session_gone_on_a_kept_consumer_is_refused_as_before(endpoint):
    tmux = endpoint["tmux"]
    server._tmux(["display-message", "-p", "-t", "=Alpha", "#{session_name}"])
    tmux("new-session", "-d", "-s", "Keep", "sleep 600")   # the server stays up
    tmux("kill-session", "-t", "=Alpha")
    assert server._tmux(["display-message", "-p", "-t", "=Alpha", "#{session_name}"]) == ""
    assert endpoint["admitted"][-1] == ("=Alpha", "capture")
    assert len(endpoint["built"]) == 1


def test_another_endpoint_in_the_environment_builds_another_consumer(endpoint, monkeypatch, tmp_path):
    server._tmux(["display-message", "-p", "-t", "=Alpha", "#{session_name}"])
    other = Path(tempfile.mkdtemp(prefix="ct-", dir=str(endpoint["sockets"].parent))).resolve()
    other.chmod(0o700)
    monkeypatch.setenv("TMUX_TMPDIR", str(other))
    server._tmux(["list-sessions", "-F", "#{session_name}"])
    assert len(endpoint["built"]) == 2


def test_a_stopped_server_with_its_socket_left_reads_as_empty(endpoint):
    """tmux 3.6 leaves the socket file after kill-server. A consumer built now
    would pin the same file, so keeping one changes nothing: the inventory is
    empty either way."""
    tmux = endpoint["tmux"]
    server._tmux(["list-sessions", "-F", "#{session_name}"])
    tmux("kill-server")
    time.sleep(0.2)
    for _ in range(2):
        assert server._tmux(["list-sessions", "-F", "#{session_name}"]) == ""
    assert len(endpoint["built"]) == 1


def test_no_socket_at_build_time_is_not_kept(endpoint):
    tmux = endpoint["tmux"]
    tmux("kill-server")
    time.sleep(0.2)
    socket = endpoint["sockets"] / f"tmux-{os.getuid()}" / "default"
    if socket.exists():
        socket.unlink()                                       # no server and no socket file
    for _ in range(2):
        assert server._tmux(["list-sessions", "-F", "#{session_name}"]) == ""
    assert len(endpoint["built"]) == 2                        # no identity: built each time, as before
    assert server._TMUX_CONSUMER_CACHE["consumer"] is None


def test_the_entry_check_runs_on_every_use_kept_or_built(endpoint):
    for _ in range(4):
        server._tmux(["display-message", "-p", "-t", "=Alpha", "#{session_name}"])
    assert len(endpoint["built"]) == 1
    assert len(endpoint["entries"]) == 4          # the build's check, also on the 3 kept uses


def test_a_missing_helper_in_an_unproven_placement_is_refused_after_caching(endpoint):
    """Cached first; then the helper disappears and a clients/ directory makes
    the placement unproven. The build refuses that; so must a kept consumer --
    inventory included, which takes no target admission."""
    assert "Alpha" in server._tmux(["list-sessions", "-F", "#{session_name}"])
    helper = endpoint["home"] / "bin/lib/runtime_tmux.py"
    aside = helper.with_suffix(".aside")
    helper.rename(aside)
    (endpoint["home"] / "clients" / "partial").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="GLOBAL_TARGET_HELPER_UNAVAILABLE"):
        server._legacy_tmux_consumer()
    assert server._tmux(["list-sessions", "-F", "#{session_name}"]) == ""
    aside.rename(helper)                          # the helper is back: a consumer again
    assert "Alpha" in server._tmux(["list-sessions", "-F", "#{session_name}"])
    assert len(endpoint["built"]) == 2


def test_a_missing_helper_in_a_pure_legacy_placement_drops_the_kept_consumer(endpoint):
    server._tmux(["list-sessions", "-F", "#{session_name}"])
    helper = endpoint["home"] / "bin/lib/runtime_tmux.py"
    aside = helper.with_suffix(".aside")
    helper.rename(aside)
    try:
        assert server._legacy_tmux_consumer() is None   # as the build answers: bare tmux
        assert server._TMUX_CONSUMER_CACHE["consumer"] is None
    finally:
        aside.rename(helper)


def test_an_alias_retargeted_to_another_server_is_another_endpoint(endpoint, monkeypatch):
    """TMUX_TMPDIR is a symlink to A (server with Alpha). It is pointed at B
    (server with Beta) while A keeps running. The resolver now finds B; a kept
    consumer must not keep reading A."""
    root = endpoint["root"]
    a, b = endpoint["sockets"], root / "b"
    b.mkdir(mode=0o700)
    alias = root / "current"
    alias.symlink_to(a)
    monkeypatch.setenv("TMUX_TMPDIR", str(alias))
    env_b = {**endpoint["env"], "TMUX_TMPDIR": str(b)}
    subprocess.run([TMUX, "new-session", "-d", "-s", "Beta", "sleep 600"], env=env_b, check=True)
    try:
        server._reset_tmux_consumer_cache()
        assert "Alpha" in server._tmux(["list-sessions", "-F", "#{session_name}"])
        assert "Alpha" in server._tmux(["list-sessions", "-F", "#{session_name}"])   # same alias: kept
        built = len(endpoint["built"])
        alias.unlink()
        alias.symlink_to(b)                       # A's server still runs
        names = server._tmux(["list-sessions", "-F", "#{session_name}"])
        assert "Beta" in names and "Alpha" not in names
        assert len(endpoint["built"]) == built + 1
    finally:
        subprocess.run([TMUX, "kill-server"], env=env_b, check=False, capture_output=True)


def test_an_alias_moved_between_the_key_and_the_build_is_not_kept(endpoint, monkeypatch):
    """The key resolves TMUX_TMPDIR, then the build resolves it again. The alias
    is moved from A to B exactly between the two: the consumer built for B must
    not be kept under A's key, and once the alias points at A again, A is read."""
    root = endpoint["root"]
    a, b = endpoint["sockets"], root / "b"
    b.mkdir(mode=0o700)
    alias = root / "current"
    alias.symlink_to(a)
    monkeypatch.setenv("TMUX_TMPDIR", str(alias))
    env_b = {**endpoint["env"], "TMUX_TMPDIR": str(b)}
    subprocess.run([TMUX, "new-session", "-d", "-s", "Beta", "sleep 600"], env=env_b, check=True)
    real_key = server._tmux_consumer_key
    moves = []

    def key_then_move():
        key = real_key()
        if not moves:
            moves.append(key)
            alias.unlink()
            alias.symlink_to(b)                   # between the key and the build
        return key

    monkeypatch.setattr(server, "_tmux_consumer_key", key_then_move)
    try:
        server._reset_tmux_consumer_cache()
        server._tmux(["list-sessions", "-F", "#{session_name}"])
        assert server._TMUX_CONSUMER_CACHE["consumer"] is None   # B's consumer was not kept under A's key
        alias.unlink()
        alias.symlink_to(a)                       # back to A
        names = server._tmux(["list-sessions", "-F", "#{session_name}"])
        assert "Alpha" in names and "Beta" not in names
        kept = server._TMUX_CONSUMER_CACHE["consumer"]
        assert kept is not None and Path(a).resolve() in Path(kept.transport.path).parents
    finally:
        subprocess.run([TMUX, "kill-server"], env=env_b, check=False, capture_output=True)


def test_the_key_names_the_exact_socket_the_resolver_picks(endpoint):
    """Not an ancestor: the one socket path. A key whose path differs anywhere
    -- another root, a root nested inside, another socket name in the same
    directory -- does not match the kept consumer."""
    server._tmux(["list-sessions", "-F", "#{session_name}"])
    consumer = server._TMUX_CONSUMER_CACHE["consumer"]
    key = server._TMUX_CONSUMER_CACHE["key"]
    socket = Path(consumer.transport.path)
    assert key[2] == str(socket) == str(endpoint["sockets"].resolve() / f"tmux-{os.getuid()}" / "default")
    assert server._consumer_matches_key(consumer, key)
    for other in (endpoint["root"] / "elsewhere" / socket.parent.name / "default",
                  socket.parent.parent / "nested" / socket.parent.name / "default",
                  socket.parent / "other"):
        assert not server._consumer_matches_key(consumer, key[:2] + (str(other),) + key[3:])


class _Kept:
    def __init__(self, path):
        self.transport = type("T", (), {"path": Path(path)})()


def test_a_socket_inside_the_key_endpoint_directory_is_not_the_key_endpoint(endpoint):
    """The ancestor test of r2 matched a consumer under a root nested inside the
    key's TMUX_TMPDIR, or another socket name there. The exact test does not."""
    server._tmux(["list-sessions", "-F", "#{session_name}"])
    key = server._TMUX_CONSUMER_CACHE["key"]
    base = endpoint["sockets"].resolve()
    nested = base / "b" / f"tmux-{os.getuid()}" / "default"
    sibling_name = base / f"tmux-{os.getuid()}" / "other"
    assert not server._consumer_matches_key(_Kept(nested), key)
    assert not server._consumer_matches_key(_Kept(sibling_name), key)


def test_an_alias_moved_to_a_root_nested_inside_the_old_one_is_another_endpoint(endpoint, monkeypatch):
    """A = <root>/a with Alpha; B = A/b, nested inside A, with Beta. The alias
    moves from A to B while A keeps running: B is read, A's consumer not kept."""
    a = endpoint["sockets"]
    b = a / "b"
    b.mkdir(mode=0o700)
    alias = endpoint["root"] / "current"
    alias.symlink_to(a)
    monkeypatch.setenv("TMUX_TMPDIR", str(alias))
    env_b = {**endpoint["env"], "TMUX_TMPDIR": str(b)}
    subprocess.run([TMUX, "new-session", "-d", "-s", "Beta", "sleep 600"], env=env_b, check=True)
    try:
        server._reset_tmux_consumer_cache()
        assert "Alpha" in server._tmux(["list-sessions", "-F", "#{session_name}"])
        alias.unlink()
        alias.symlink_to(b)
        names = server._tmux(["list-sessions", "-F", "#{session_name}"])
        assert "Beta" in names and "Alpha" not in names
        kept = server._TMUX_CONSUMER_CACHE["consumer"]
        assert str(kept.transport.path) == str(b.resolve() / f"tmux-{os.getuid()}" / "default")
    finally:
        subprocess.run([TMUX, "kill-server"], env=env_b, check=False, capture_output=True)


def test_an_inherited_tmux_names_its_own_socket(endpoint, monkeypatch):
    sock = endpoint["sockets"].resolve() / f"tmux-{os.getuid()}" / "default"
    pid = endpoint["tmux"]("display-message", "-p", "#{pid}").stdout.strip()
    monkeypatch.setenv("TMUX", f"{sock},{pid},0")
    server._reset_tmux_consumer_cache()
    assert "Alpha" in server._tmux(["list-sessions", "-F", "#{session_name}"])
    key = server._TMUX_CONSUMER_CACHE["key"]
    assert key[2] == str(sock) and key[3] == pid
