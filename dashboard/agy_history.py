"""Bind a managed interactive agy launch to its official hook transcript."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile
from uuid import uuid4

try:
    from .dsh_history import _read, unbound
except ImportError:  # Installed dashboard or CLI entry point.
    from dsh_history import _read, unbound


PROGRAMS = frozenset({"agy", "antigravity", "antigravity-cli", "antigravity-agent"})
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,95}\Z")
UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z")
LAUNCH = re.compile(r"[a-f0-9]{32}\Z")
TRANSCRIPTS = frozenset({"transcript.jsonl", "transcript_full.jsonl"})
GROUP = "orrery-reader"
HOOKS = ("SessionStart", "PreInvocation")


def _atomic_json(path, data):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix="." + path.name + "-", delete=False) as temporary:
        temp_path = Path(temporary.name)
        try:
            json.dump(data, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


def _index(runtime, name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("invalid agent name")
    return Path(runtime) / "agy-history" / f"{name}.json"


def _receipt(index, launch):
    nonce = launch.get("launch_id")
    if not isinstance(nonce, str) or not LAUNCH.fullmatch(nonce):
        raise ValueError("invalid launch")
    return index.with_name(f"{index.stem}-{nonce}.json")


@contextmanager
def _receipt_lock(path):
    import fcntl  # The receipt writer is POSIX-only; keep Windows imports of the dashboard safe.
    descriptor = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def prepare(runtime, home, name, agent_id, project_key, workspace, tmux):
    target, session_id, created, pid = tmux.split("|")
    if (name != target or not re.fullmatch(r"\$[0-9]+", session_id)
            or int(created) <= 0 or int(pid) <= 0 or int(agent_id) <= 0 or not project_key):
        raise ValueError("invalid session identity")
    index = _index(runtime, name)
    index.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = index.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("invalid history directory")
    launch_id = uuid4().hex
    _atomic_json(index, dict(
        schema=1, agent_name=name, agent_id=int(agent_id), project_key=project_key,
        launch_id=launch_id, session_id=session_id, created=int(created), tmux_pid=int(pid),
        home=str(Path(home).resolve()), workspace=str(Path(workspace).resolve()),
    ))
    return launch_id


def _transcript(home, sid, value):
    if not isinstance(sid, str) or not UUID.fullmatch(sid):
        raise ValueError("invalid conversation UUID")
    if not isinstance(value, str):
        raise ValueError("unexpected transcript path")
    path = Path(value).expanduser()
    expected_dir = Path(home) / ".gemini/antigravity-cli/brain" / sid / ".system_generated/logs"
    if path.parent != expected_dir or path.name not in TRANSCRIPTS:
        raise ValueError("unexpected transcript path")
    return path


def record(runtime, name, nonce, payload):
    if not nonce or not isinstance(payload, dict):
        return
    index = _index(runtime, name)
    launch = _read(index)
    if launch.get("launch_id") != nonce:
        return
    workspaces = payload.get("workspacePaths")
    if not isinstance(workspaces, list) or launch["workspace"] not in workspaces:
        return
    sid = payload.get("conversationId")
    transcript = _transcript(launch["home"], sid, payload.get("transcriptPath"))
    path = _receipt(index, launch)
    with _receipt_lock(path):
        try:
            existing = _read(path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing.get("conflicted") is not True and existing.get("conversation_id") != sid:
                # The hook does not identify top-level vs subagent. Another
                # UUID in this launch must never replace the bound history.
                _atomic_json(path, dict(conflicted=True))
            return
        evidence = dict(launch, conversation_id=sid, transcript_path=str(transcript),
                        received_at=datetime.now(timezone.utc).isoformat())
        _atomic_json(path, evidence)


def binding(runtime, name, registration, session=None):
    if not registration or registration.get("program") not in PROGRAMS:
        return unbound("registration_missing")
    try:
        index = _index(runtime, name)
        launch = _read(index)
        if (launch.get("schema") != 1 or launch.get("agent_name") != name
                or launch.get("agent_id") != registration.get("agent_id")
                or launch.get("project_key") != registration.get("project_key")):
            return unbound("identity_mismatch")
        if session is not None and any(launch.get(k) != session.get(k)
                                       for k in ("session_id", "created", "tmux_pid")):
            return dict(unbound("receipt_predates_session"), history_binding="stale")
        path = _receipt(index, launch)
        receipt = _read(path)
        if receipt.get("conflicted") is True:
            return unbound("session_conflict")
        if any(receipt.get(key) != value for key, value in launch.items()):
            return unbound("launch_mismatch")
        sid = receipt["conversation_id"]
        transcript = _transcript(launch["home"], sid, receipt["transcript_path"])
        info = _read(transcript, metadata=False)
        if not info.st_size:
            return unbound("receipt_unavailable")
        timestamp = receipt["received_at"]
        when = datetime.fromisoformat(timestamp)
        if when.tzinfo is None or when.timestamp() < launch["created"]:
            return unbound("invalid_receipt")
        if _read(index) != launch or _read(path) != receipt:
            return unbound("launch_mismatch")
        return dict(history_binding="bound", history_binding_reason="",
                    history_binding_reason_code="bound", transcript_path=str(transcript),
                    history_session_id=sid, history_receipt_ts=timestamp)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return unbound("receipt_unavailable")


def configure(path, mode):
    """Modify the single owned hook group in the existing provider config."""
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), "hook"])
    expected = {event: [{"type": "command", "command": command, "timeout": 2}] for event in HOOKS}
    try:
        config = _read(path)
    except FileNotFoundError:
        if mode == "uninstall":
            return
        config = {}
    current = config.get(GROUP)
    if GROUP in config and current != expected:
        # Honor edited or disabled rules. Setup only owns its exact group.
        raise ValueError("existing ORRERY Reader hook group differs from the managed group")
    if mode == "install" and current is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        config[GROUP] = expected
        _atomic_json(path, config)
    elif mode == "uninstall" and current is not None:
        del config[GROUP]
        _atomic_json(path, config)


def main():
    command = sys.argv[1]
    if command == "configure":
        configure(Path(sys.argv[3]).expanduser().absolute(), sys.argv[2])
        return
    runtime = Path(os.environ.get("AGENTSTACK_RUNTIME_DIR") or Path.home() / ".agentstack/runtime").resolve()
    name = os.environ.get("AGENT_NAME")
    if command == "prepare":
        print(prepare(runtime, Path.home(), name, sys.argv[2], os.environ.get("AGENTSTACK_PROJECT_KEY", ""),
                      Path.cwd(), sys.argv[3]))
    elif command == "hook":
        try:
            nonce = os.environ.get("AGENTSTACK_AGY_HISTORY_LAUNCH")
            if nonce:
                record(runtime, name, nonce, json.loads(sys.stdin.buffer.read(256 * 1024 + 1)))
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Reader availability must not interrupt the model invocation.
        print("{}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(f"Antigravity Reader: {error}")
