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
FIRST_TASK_LIMIT = 2 * 1024 * 1024


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


def _resume_proof(path):
    return path.with_suffix(".resume.json")


def confirm_resume(runtime, name, nonce, sid, previous):
    """Carry the verified transcript into the new launch after native MCP is ready."""
    index = _index(runtime, name)
    launch = _read(index)
    if (launch.get("launch_id") != nonce or previous.get("history_binding") != "bound"
            or previous.get("history_session_id") != sid
            or previous.get("history_workspace") != launch.get("workspace")):
        raise ValueError("Antigravity's resume no longer matches its saved conversation")
    transcript = _transcript(launch["home"], sid, previous["transcript_path"])
    if not _read(transcript, metadata=False).st_size:
        raise ValueError("Antigravity's saved transcript is empty")
    path = _receipt(index, launch)
    with _receipt_lock(path):
        if _read(index) != launch:
            raise ValueError("Antigravity's launch changed before MCP became ready")
        try:
            receipt = _read(path)
        except FileNotFoundError:
            receipt = None
        if receipt is not None and receipt.get("conversation_id") != sid:
            raise ValueError("Antigravity restored a different conversation")
        _atomic_json(_resume_proof(path), dict(
            launch, conversation_id=sid, transcript_path=str(transcript),
            received_at=datetime.now(timezone.utc).isoformat(),
        ))


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
        try:
            resume = _read(_resume_proof(path))
        except FileNotFoundError:
            resume = None
        if resume is not None and resume.get("conversation_id") != sid:
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
        try:
            receipt = _read(path)
        except FileNotFoundError:
            proof = _resume_proof(path)
            receipt = _read(proof)
            if path.exists():
                receipt = _read(path)
            else:
                path = proof
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
                    history_session_id=sid, history_receipt_ts=timestamp,
                    history_workspace=launch["workspace"])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return unbound("receipt_unavailable")


def resume_workspace(runtime, name, registration, conversation, *, session=None):
    index = _index(runtime, name)
    launch = _read(index)
    receipt = binding(runtime, name, registration, session)
    if (receipt.get("history_binding") != "bound" or receipt.get("history_session_id") != conversation
            or _read(index) != launch):
        raise ValueError("Antigravity's original workspace and conversation could not be verified")
    cwd = Path(launch["workspace"])
    if not cwd.is_absolute() or cwd.resolve(strict=True) != cwd or not cwd.is_dir():
        raise ValueError("Antigravity's original workspace is unavailable")
    return cwd


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


def first_task(runtime, name, nonce, payload, environment):
    trigger = environment.get("AGENTSTACK_AGY_FIRST_TASK_TRIGGER")
    spool = environment.get("AGENTSTACK_AGY_FIRST_TASK_DIR")
    if (not trigger or not spool or not isinstance(payload, dict)
            or payload.get("lastUserInput") != trigger or payload.get("invocationNum") != 0):
        return None
    launch = _read(_index(runtime, name))
    workspaces = payload.get("workspacePaths")
    if (launch.get("launch_id") != nonce or not isinstance(workspaces, list)
            or launch["workspace"] not in workspaces):
        raise ValueError("Antigravity first task does not match this launch")
    folder = Path(spool)
    info = folder.lstat()
    if (folder.parent != runtime or not folder.name.startswith("agy-first-task-")
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("Antigravity first task directory is not private")
    # Only one invocation can consume the original path.
    claimed = folder / "claimed"
    try:
        (folder / "payload").replace(claimed)
    except FileNotFoundError:
        return None
    descriptor = os.open(claimed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Antigravity first task is not a private file")
        content = source.read(FIRST_TASK_LIMIT + 1)
    if not content or len(content) > FIRST_TASK_LIMIT:
        raise ValueError("Antigravity first task has an invalid size")
    return {"injectSteps": [{"userMessage": content.decode("utf-8")}]}, folder / "acknowledged"


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
        nonce = os.environ.get("AGENTSTACK_AGY_HISTORY_LAUNCH")
        if not nonce:
            print("{}")
            return
        try:
            payload = json.loads(sys.stdin.buffer.read(256 * 1024 + 1))
        except (OSError, ValueError, TypeError):
            print("{}")
            return
        try:
            record(runtime, name, nonce, payload)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Reader availability must not interrupt the model invocation.
        try:
            pending = first_task(runtime, name, nonce, payload, os.environ)
        except (OSError, ValueError, KeyError, TypeError):
            print(json.dumps({"terminationBehavior": "terminate", "injectSteps": [
                {"errorMessage": {"userMessage": "ORRERY could not verify Antigravity's first task."}}
            ]}))
            return
        if pending is None:
            print("{}")
            return
        output, receipt = pending
        print(json.dumps(output, ensure_ascii=False), flush=True)
        receipt.touch(mode=0o600, exist_ok=False)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(f"Antigravity Reader: {error}")
