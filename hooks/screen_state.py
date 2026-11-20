"""Private launch-generation state for orrery-screen, shared by launchers and HTTP."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

SERVER = "orrery-screen"
TOOLS = (
    "check_permissions", "list_apps", "list_windows", "get_window_state",
    "launch_app", "click", "type_text", "press_key", "hotkey", "scroll", "set_value", "invoke_menu",
)
MODES = ("off", "view", "control")
TARGET = ("project", "runtime", "agent", "generation")
OPERATIONS = ("ok", "denied", "upstream_error")
MAX_RECENT = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS current_launch (
    project TEXT NOT NULL, runtime TEXT NOT NULL, agent TEXT NOT NULL,
    generation TEXT NOT NULL, credential TEXT UNIQUE,
    computer_use TEXT NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY (project, runtime, agent)
);
CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY, project TEXT NOT NULL, runtime TEXT NOT NULL,
    agent TEXT NOT NULL, generation TEXT NOT NULL, tool TEXT NOT NULL,
    started TEXT NOT NULL, finished TEXT, result TEXT
);
"""
NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


class ScreenStateError(ValueError):
    def __init__(self, reason, status=400):
        super().__init__(reason)
        self.status = status


def _id(value, regexp):
    return isinstance(value, str) and re.fullmatch(regexp, value) is not None


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _validated_target(target):
    if (not isinstance(target, dict) or set(target) != set(TARGET)
            or not isinstance(target["project"], str) or not 0 < len(target["project"]) <= 4096
            or not _id(target["runtime"], r"[0-9a-f]{32}")
            or not _id(target["agent"], r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
            or not _id(target["generation"], r"[0-9a-f]{32}|[0-9a-f-]{36}")):
        raise ScreenStateError("SCREEN_TARGET_INVALID")
    return tuple(target[key] for key in TARGET)


class Store:
    def __init__(self, runtime_dir):
        self.directory = Path(runtime_dir).resolve() / "computer-use"
        self.path = self.directory / "state.sqlite3"

    @contextmanager
    def connect(self, *, create=False, write=False):
        if create:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            info = self.directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise ScreenStateError("SCREEN_STATE_UNAVAILABLE", 503)
            if create:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise ScreenStateError("SCREEN_STATE_UNAVAILABLE", 503)
        except FileNotFoundError:
            raise ScreenStateError("SCREEN_TARGET_UNAVAILABLE", 404) from None
        conn = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write or create else "?mode=ro"),
                               uri=True, timeout=0.75)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                if create:
                    conn.executescript(SCHEMA)
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    def launch(self, project, runtime, agent, generation, mode):
        if mode is None and not self.path.exists():
            return None
        target = dict(zip(TARGET, (project, runtime, agent, generation)))
        identity = _validated_target(target)
        if mode is not None and mode not in MODES:
            raise ScreenStateError("SCREEN_MODE_INVALID")
        credential = secrets.token_urlsafe(32) if mode is not None else None
        with self.connect(create=mode is not None, write=True) as db:
            old = db.execute("""
                SELECT project, runtime, agent, generation FROM current_launch WHERE runtime=? AND agent=?
            """, (runtime, agent)).fetchall()
            db.execute("DELETE FROM current_launch WHERE runtime=? AND agent=?", (runtime, agent))
            if credential is not None:
                db.execute("INSERT INTO current_launch VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (*identity, _digest(credential), mode, "default" if mode == "off" else "launch"))
        for previous in old:
            binding_path(self, dict(previous)).unlink(missing_ok=True)
        return credential

    def revoke(self, target):
        if not self.path.exists():
            return
        with self.connect(write=True) as db:
            db.execute("DELETE FROM current_launch WHERE project=? AND runtime=? AND agent=? AND generation=?",
                       _validated_target(target))
        binding_path(self, target).unlink(missing_ok=True)

    def list(self):
        if not self.path.exists():
            return []
        with self.connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT project, runtime, agent, generation, computer_use, source
                FROM current_launch ORDER BY project, agent, runtime
            """)]

    @staticmethod
    def _target(db, identity):
        row = db.execute("""
            SELECT project, runtime, agent, generation, computer_use, source
            FROM current_launch WHERE project=? AND runtime=? AND agent=? AND generation=?
        """, identity).fetchone()
        if row is None:
            raise ScreenStateError("SCREEN_TARGET_UNAVAILABLE", 404)
        return dict(row)

    def get(self, target):
        with self.connect() as db:
            return self._target(db, _validated_target(target))

    def change(self, target, mode):
        identity = _validated_target(target)
        if mode not in MODES:
            raise ScreenStateError("SCREEN_MODE_INVALID")
        with self.connect(write=True) as db:
            self._target(db, identity)
            db.execute("""
                UPDATE current_launch SET computer_use=?, source='operator'
                WHERE project=? AND runtime=? AND agent=? AND generation=?
            """, (mode, *identity))
            return self._target(db, identity)

    @staticmethod
    def _child(db, credential):
        row = db.execute("""
            SELECT project, runtime, agent, generation, computer_use, source
            FROM current_launch WHERE credential=?
        """, (_digest(credential),)).fetchone()
        if row is None:
            raise ScreenStateError("SCREEN_CHILD_REQUIRED", 403)
        return dict(row)

    @staticmethod
    def validate_token(credential):
        if not _id(credential, r"[A-Za-z0-9_-]{43}"):
            raise ScreenStateError("SCREEN_CHILD_REQUIRED", 403)

    def child(self, credential):
        self.validate_token(credential)
        with self.connect() as db:
            state = self._child(db, credential)
        del state["agent"]
        return state

    def log(self, credential, body):
        self.validate_token(credential)
        if (not isinstance(body, dict) or set(body) != {"call_id", "tool", "phase", "result"}
                or not _id(body["call_id"], r"[0-9a-f]{32}")
                or body["tool"] not in (*TOOLS, "unknown")
                or not ((body["phase"] == "start" and body["result"] is None)
                        or (body["phase"] == "finish" and body["result"] in OPERATIONS))):
            raise ScreenStateError("SCREEN_EVENT_INVALID")
        call_id, tool = body["call_id"], body["tool"]
        with self.connect(write=True) as db:
            state = self._child(db, credential)
            target = tuple(state[key] for key in TARGET)
            if body["phase"] == "start":
                db.execute(f"""
                    INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, {NOW_SQL}, NULL, NULL)
                    ON CONFLICT(call_id) DO NOTHING
                """, (call_id, *target, tool))
                db.execute("""
                    DELETE FROM calls WHERE call_id IN
                        (SELECT call_id FROM calls ORDER BY rowid DESC LIMIT -1 OFFSET ?)
                """, (MAX_RECENT,))
            else:
                result = db.execute(f"""
                    UPDATE calls SET finished={NOW_SQL}, result=?
                    WHERE call_id=? AND project=? AND runtime=? AND agent=? AND generation=?
                        AND tool=? AND finished IS NULL
                """, (body["result"], call_id, *target, tool))
                if result.rowcount != 1:
                    raise ScreenStateError("SCREEN_EVENT_UNAVAILABLE", 409)

    def recent(self, target):
        identity = _validated_target(target)
        with self.connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT call_id, agent, tool, started, finished, result
                FROM calls WHERE project=? AND runtime=? AND agent=? AND generation=?
                ORDER BY rowid DESC
            """, identity)]


def launch_runtime(runtime_dir):
    return _digest(str(Path(runtime_dir).resolve()))[:32]


def _deployment_python(mail_env):
    if not mail_env:
        return None
    env = Path(mail_env).resolve()
    try:
        fd = os.open(env.with_name("deployment.json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            mode = os.fstat(stream.fileno())
            if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
                return None
            raw = stream.read(32769)
        if len(raw) > 32768:
            return None
        deployment = json.loads(raw)
        candidate = Path(deployment["candidate_venv"])
        if (deployment.get("kind") == "orrery-mail-deployment-v1"
                and deployment.get("service_env") == str(env) and candidate.is_absolute()
                and deployment.get("enroll_bin") == str(candidate / "bin/agentstack-enroll")):
            return str(candidate / "bin/python")
    except (OSError, TypeError, ValueError, KeyError):
        return None
    return None


def _python(mail_env):
    explicit = os.environ.get("AGENTSTACK_SCREEN_PYTHON")
    enroll = os.environ.get("AGENTSTACK_MAIL_ENROLL_BIN")
    candidates = [explicit] if explicit else [
        os.environ.get("AGENTSTACK_FRESH_PYTHON"), sys.executable,
        str(Path(enroll).with_name("python")) if enroll else None,
        _deployment_python(mail_env or os.environ.get("AGENTSTACK_MAIL_ENV")),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        binary = shutil.which(candidate)
        if not binary or (explicit and not Path(explicit).is_absolute()):
            continue
        try:
            result = subprocess.run([binary, "-I", "-c", "from agentstack_mail import screen_mcp"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return binary
    raise ScreenStateError("SCREEN_RELAY_PYTHON_UNAVAILABLE", 503)


def binding_path(store, target):
    return store.directory / ("binding-" + _digest(json.dumps(target, sort_keys=True)) + ".json")


def definition(runtime_dir, project, agent, generation, mode, *, scope=None, origin=None, mail_env=None):
    runtime = launch_runtime(scope or runtime_dir)
    if mode is None:
        Store(runtime_dir).launch(project, runtime, agent, generation, mode)
        return None
    binary = _python(mail_env)
    origin = origin or "http://127.0.0.1:" + os.environ.get("AGENTSTACK_PORT", "8770")
    endpoint = urlsplit(origin)
    if (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port
            or endpoint.username or endpoint.path or endpoint.query or endpoint.fragment):
        raise ScreenStateError("SCREEN_ORIGIN_INVALID")
    store = Store(runtime_dir)
    target = dict(zip(TARGET, (project, runtime, agent, generation)))
    binding_file = binding_path(store, target)
    try:
        store.get(target)
    except ScreenStateError as exc:
        if exc.status != 404:
            raise
        credential = store.launch(project, runtime, agent, generation, mode)
        binding = dict(project=project, runtime=runtime, generation=generation,
                       origin=origin, credential=credential)
        try:
            fd, temporary = tempfile.mkstemp(dir=store.directory, prefix=".binding-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(binding, handle, ensure_ascii=False)
                os.replace(temporary, binding_file)
            finally:
                Path(temporary).unlink(missing_ok=True)
        except OSError:
            store.revoke(target)
            raise
    if not binding_file.is_file():
        raise ScreenStateError("SCREEN_BINDING_UNAVAILABLE", 503)
    env = {"AGENTSTACK_SCREEN_BINDING_FILE": str(binding_file)}
    for key in ("AGENTSTACK_CUA_DRIVER", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP",
                "WSL_INTEROP", "WSL_DISTRO_NAME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return {"command": binary, "args": ["-I", "-m", "agentstack_mail.screen_mcp"], "env": env}
