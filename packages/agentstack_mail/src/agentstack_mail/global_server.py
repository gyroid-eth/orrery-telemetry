"""Explicit isolated S1 server; the default v1 application is unchanged.

Runtime authority epochs are independent of candidate generation and mutation
revision. This is a cooperating-writer fence, not OS supervisor control.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
import fcntl
import hashlib
import hmac
import json
import os
import pwd
from pathlib import Path
import secrets
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
import uuid

from .boundary import CompatibilityFastMCP
from .enrollment import _peer_uid
from .namespace_state_io import connection
from .namespace_store import changed, generation
from .utils import sanitize_agent_name

TOOLS = frozenset(
    {
        "health_check",
        "ensure_project",
        "register_agent",
        "whois",
        "fetch_inbox",
        "retire_agent",
        "unretire_agent",
    }
)
WIRE_VERSION = 1


class GlobalError(ValueError):
    """Bounded errors never include credentials, SQL, paths or peer data."""


def private(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise GlobalError("PRIVATE_FILE_UNSAFE")
            raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise GlobalError("PRIVATE_FILE_TOO_LARGE")
            return raw
    except OSError:
        raise GlobalError("PRIVATE_FILE_UNAVAILABLE") from None


def document(path):
    try:
        value = json.loads(private(path))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise GlobalError("DOCUMENT_INVALID") from None


def integer(value, minimum=1):
    return type(value) is int and value >= minimum


def fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16] if token else None


def now():
    return datetime.now(timezone.utc).isoformat()


def instant(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return date if date.tzinfo is not None else date.replace(tzinfo=timezone.utc)


class GlobalRuntime:
    config_kind = "orrery-global-server-s1"
    schema_version = 2
    tools = TOOLS

    def __init__(self, config):
        self.config_path = Path(config)
        self.config_raw = private(self.config_path)
        self.config = document(self.config_path)
        cfg = self.config
        if (
            cfg.get("kind") != self.config_kind
            or cfg.get("activation_enabled") is not False
        ):
            raise GlobalError("ISOLATED_CONFIG_REQUIRED")
        root = Path(cfg.get("isolation_root", ""))
        temp_roots = [
            Path(tempfile.gettempdir()).resolve(),
            Path("/private/tmp").resolve(),
        ]
        if (
            not root.is_absolute()
            or root.is_symlink()
            or not any(
                t == root.resolve() or t in root.resolve().parents for t in temp_roots
            )
        ):
            raise GlobalError("ISOLATION_ROOT_INVALID")
        info = root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise GlobalError("ISOLATION_ROOT_UNSAFE")
        self.root = root.resolve()
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        if self.root == real_home or real_home in self.root.parents:
            raise GlobalError("REAL_HOME_REFUSED")
        runtime_root = cfg.get("runtime_root")
        if not isinstance(runtime_root, str) or not Path(runtime_root).is_absolute():
            raise GlobalError("RUNTIME_ROOT_REQUIRED")
        self.runtime_root = Path(runtime_root).resolve()
        if self.root not in self.runtime_root.parents:
            raise GlobalError("RUNTIME_ROOT_OUTSIDE_ISOLATION")
        self.paths = {}
        for key in ("database", "authority", "authority_lock", "management_socket"):
            raw = cfg.get(key)
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                raise GlobalError("CONFIG_PATH_INVALID")
            path = Path(raw)
            if path.is_symlink() or self.root not in path.resolve().parents:
                raise GlobalError("CONFIG_PATH_OUTSIDE_ISOLATION")
            self.paths[key] = path
        if self.runtime_root not in self.paths["database"].resolve().parents:
            raise GlobalError("DATABASE_OUTSIDE_RUNTIME_ROOT")
        self.initial_preflight()
        db_fd = os.open(self.paths["database"], os.O_RDONLY | os.O_NOFOLLOW)
        try:
            db_info = os.fstat(db_fd)
            if (
                not stat.S_ISREG(db_info.st_mode)
                or db_info.st_uid != os.getuid()
                or db_info.st_mode & 0o077
                or db_info.st_nlink != 1
            ):
                raise GlobalError("DATABASE_UNSAFE")
            self.db_identity = (db_info.st_dev, db_info.st_ino)
        finally:
            os.close(db_fd)
        self.check_sqlite_files()
        try:
            with connection(self.paths["database"]) as db:
                self.check_sqlite_files()
                if self.schema_version == 3:
                    db.execute("BEGIN DEFERRED")
                metadata = dict(db.execute("SELECT key,value FROM namespace_metadata"))
                instances = list(db.execute("SELECT instance_id FROM mail_instances"))
                columns = {row[1] for row in db.execute("PRAGMA table_info(agents)")}
                message_columns = {
                    row[1] for row in db.execute("PRAGMA table_info(messages)")
                }
                if (
                    db.execute("PRAGMA user_version").fetchone()[0]
                    != self.schema_version
                    or metadata.get("activation") != "false"
                    or len(instances) != 1
                    or not {"lookup_key", "legacy_project_id", "credential_generation"}
                    <= columns
                    or "project_id" in columns
                    or "reply_to" not in message_columns
                    or db.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                    or db.execute("PRAGMA foreign_key_check").fetchall()
                ):
                    raise GlobalError("CANDIDATE_SCHEMA_REQUIRED")
                self.instance = instances[0][0]
                self.candidate_generation = metadata["generation"]
                if not self.candidate_generation or not self.instance:
                    raise GlobalError("CANDIDATE_SCHEMA_REQUIRED")
        except (sqlite3.Error, KeyError):
            raise GlobalError("CANDIDATE_SCHEMA_REQUIRED") from None
        self.epoch = cfg.get("authority_epoch")
        if not isinstance(self.epoch, str) or not self.epoch or len(self.epoch) > 128:
            raise GlobalError("AUTHORITY_EPOCH_INVALID")
        if (
            cfg.get("candidate_generation") != self.candidate_generation
            or cfg.get("mail_instance_id") != self.instance
        ):
            raise GlobalError("CANDIDATE_BINDING_MISMATCH")
        # Pin the first validated lock inode before this runtime is published.
        # Every later operation must share the updater's same lock object.
        self.lock_identity = None
        with self.transaction():
            pass
        self.control = None
        self.handlers = set()
        self.writers = set()

    def authority(self):
        value = document(self.paths["authority"])
        if (
            value.get("kind") != "orrery-global-authority-v1"
            or value.get("phase") != "active"
            or value.get("root_status") != "active"
            or value.get("runtime_root") != str(self.runtime_root)
            or value.get("mail_instance_id") != self.instance
            or value.get("candidate_generation") != self.candidate_generation
            or value.get("authority_epoch") != self.epoch
        ):
            raise GlobalError("WRITER_FENCED")
        return value

    def check_sqlite_files(self):
        def safe(info):
            return (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and not info.st_mode & 0o077
                and info.st_nlink == 1
            )

        # Recheck the main database too: its links/mode can change while its
        # inode remains pinned. SQLite still reopens names, so this cannot be
        # an atomic filesystem boundary against non-cooperating same-UID code.
        for suffix in ("", "-wal", "-shm", "-journal"):
            reason = "SQLITE_SIDECAR_UNSAFE" if suffix else "DATABASE_UNSAFE"
            path = Path(str(self.paths["database"]) + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                if suffix:
                    continue
                raise GlobalError(reason) from None
            if not suffix and self.db_identity != (info.st_dev, info.st_ino):
                raise GlobalError("DATABASE_REPLACED")
            if not safe(info):
                raise GlobalError(reason)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    opened = os.fstat(fd)
                    current = path.lstat()
                    if (
                        (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                        or not safe(opened)
                        or not safe(current)
                    ):
                        raise GlobalError(reason)
                finally:
                    os.close(fd)
            except OSError:
                raise GlobalError(reason) from None

    @contextmanager
    def transaction(self, *, write=False, binding=None):
        try:
            fd = os.open(
                self.paths["authority_lock"],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
        except OSError:
            raise GlobalError("AUTHORITY_LOCK_UNAVAILABLE") from None
        try:
            self.check_authority_lock(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise GlobalError("WRITER_FENCED") from None
            before = self.authority()
            self.check_authority_lock(fd)
            if private(self.config_path) != self.config_raw:
                raise GlobalError("CONFIG_CHANGED")
            if binding is not None:
                expected = (self.instance, self.candidate_generation, self.epoch)
                if tuple(binding) != expected:
                    raise GlobalError("STALE_RUNTIME_BINDING")
            database_info = self.paths["database"].lstat()
            if self.db_identity != (database_info.st_dev, database_info.st_ino):
                raise GlobalError("DATABASE_REPLACED")
            self.check_sqlite_files()
            with connection(self.paths["database"], write=write) as db:
                # Narrow the name-reopen window before issuing operation SQL.
                self.check_sqlite_files()
                if write:
                    db.execute("BEGIN IMMEDIATE")
                elif self.schema_version == 3:
                    db.execute("BEGIN DEFERRED")
                if generation(db) != self.candidate_generation:
                    raise GlobalError("CANDIDATE_CHANGED")
                self.before_operation(db, write=write)
                yield db
                self.after_operation(db, write=write)
                # These metadata constraints can change without replacing an
                # inode. Detect them before the connection context commits.
                self.check_sqlite_files()
                self.check_authority_lock(fd)
                current_db = self.paths["database"].lstat()
                if (
                    self.authority() != before
                    or private(self.config_path) != self.config_raw
                    or self.db_identity != (current_db.st_dev, current_db.st_ino)
                ):
                    raise GlobalError("WRITER_FENCED")
        except (sqlite3.Error, OSError, KeyError):
            raise GlobalError("STATE_UNAVAILABLE") from None
        finally:
            os.close(fd)

    def check_authority_lock(self, fd):
        opened = os.fstat(fd)
        current = self.paths["authority_lock"].lstat()
        for info in (opened, current):
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise GlobalError("AUTHORITY_LOCK_UNSAFE")
        identity = (opened.st_dev, opened.st_ino)
        if self.lock_identity is None:
            self.lock_identity = identity
        if identity != self.lock_identity or identity != (current.st_dev, current.st_ino):
            raise GlobalError("AUTHORITY_LOCK_REPLACED")

    def before_operation(self, db, *, write):
        pass

    def initial_preflight(self):
        pass

    def after_operation(self, db, *, write):
        pass

    def owner(self, db, agent_id, token, *, retired=False):
        if not integer(agent_id) or not isinstance(token, str) or not token:
            raise GlobalError("OWNER_REQUIRED")
        row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        try:
            token_matches = (
                row is not None
                and isinstance(row["registration_token"], str)
                and hmac.compare_digest(
                    row["registration_token"].encode("utf-8"), token.encode("utf-8")
                )
            )
        except UnicodeEncodeError:
            token_matches = False
        if (
            row is None
            or not token_matches
            or (row["retired_at"] is not None and not retired)
        ):
            raise GlobalError("OWNER_REQUIRED")
        return row

    def identity(self, row):
        return {
            "id": row["id"],
            "agent_id": row["id"],
            "name": row["name"],
            "credential_generation": row["credential_generation"],
            "retired_at": row["retired_at"],
            "server_instance_id": self.instance,
            "candidate_generation": self.candidate_generation,
            "authority_epoch": self.epoch,
        }

    def register(
        self,
        binding,
        agent_id,
        name,
        program,
        model,
        task,
        token,
        window_row,
        window_uuid,
    ):
        if (
            not isinstance(program, str)
            or not program
            or not isinstance(model, str)
            or not model
        ):
            raise GlobalError("PROGRAM_MODEL_REQUIRED")
        with self.transaction(write=True, binding=binding) as db:
            if agent_id is None:
                if window_row is not None or window_uuid is not None:
                    raise GlobalError("EXISTING_WINDOW_REQUIRED")
                if (
                    not isinstance(name, str)
                    or sanitize_agent_name(name) != name
                    or not name
                ):
                    raise GlobalError("NAME_INVALID")
                if db.execute(
                    "SELECT 1 FROM agents WHERE lookup_key=?", (name.lower(),)
                ).fetchone():
                    raise GlobalError("NAME_CONFLICT")
                if token is not None and (
                    not isinstance(token, str)
                    or not 20 <= len(token) <= 64
                    or any(c.isspace() for c in token)
                ):
                    raise GlobalError("CREDENTIAL_INVALID")
                token = token or secrets.token_urlsafe(32)
                stamp = now()
                db.execute(
                    "INSERT INTO agents(name,lookup_key,program,model,task_description,inception_ts,last_active_ts,attachments_policy,contact_policy,registration_token,credential_generation) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        name,
                        name.lower(),
                        program,
                        model,
                        task,
                        stamp,
                        stamp,
                        "auto",
                        "open",
                        token,
                        1,
                    ),
                )
                agent_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            else:
                row = self.owner(db, agent_id, token)
                # A stable ID decides the owner after an approved rename. The
                # deprecated spelling is display metadata, not routing.
                if (window_row is None) != (window_uuid is None):
                    raise GlobalError("WINDOW_INPUT_REQUIRED")
                if window_row is not None:
                    window = db.execute(
                        "SELECT agent_id,window_uuid FROM window_identities WHERE id=?",
                        (window_row,),
                    ).fetchone()
                    if (
                        window is None
                        or window["agent_id"] != agent_id
                        or window["window_uuid"] != window_uuid
                    ):
                        raise GlobalError("WINDOW_OWNER_MISMATCH")
                db.execute(
                    "UPDATE agents SET program=?,model=?,task_description=?,last_active_ts=? WHERE id=?",
                    (program, model, task, now(), agent_id),
                )
            changed(db)
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            result = self.identity(row)
            result["registration_token"] = token
            if window_row is not None:
                result.update(window_row_id=window_row, window_uuid=window_uuid)
            return result

    def inspect(self, db, agent_id):
        if not integer(agent_id):
            raise GlobalError("AGENT_ID_REQUIRED")
        row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if row is None:
            raise GlobalError("AGENT_NOT_FOUND")
        return {
            "ok": True,
            **self.identity(row),
            "credential_fingerprint": fingerprint(row["registration_token"]),
            "credential_state": (
                "server-token" if row["registration_token"] else "server-null"
            ),
            "connection_pinned": True,
        }

    def management(self, request, peer_uid):
        try:
            return self._management_apply(request, peer_uid)
        except GlobalError as exc:
            if isinstance(request, dict) and request.get("action") in {
                "claim",
                "recover",
            }:
                binding = [
                    request.get(k)
                    for k in (
                        "expected_server_instance_id",
                        "candidate_generation",
                        "authority_epoch",
                    )
                ]
                # A rejected CAS is audited under the same valid authority.
                # Fenced/unknown callers cannot obtain a side-channel write.
                with suppress(GlobalError):
                    with self.transaction(write=True, binding=binding) as db:
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS global_enrollment_audit(id INTEGER PRIMARY KEY,request_id TEXT NOT NULL,created_ts TEXT NOT NULL,receipt TEXT NOT NULL)"
                        )
                        request_id = request.get("request_id")
                        receipt = {
                            "ok": False,
                            "reason": str(exc),
                            "peer_uid": peer_uid,
                            "agent_id": (
                                request.get("agent_id")
                                if integer(request.get("agent_id"))
                                else None
                            ),
                        }
                        db.execute(
                            "INSERT INTO global_enrollment_audit(request_id,created_ts,receipt) VALUES (?,?,?)",
                            (
                                (
                                    request_id
                                    if isinstance(request_id, str)
                                    and len(request_id) <= 64
                                    else "invalid"
                                ),
                                now(),
                                json.dumps(receipt),
                            ),
                        )
                        changed(db)
            raise

    def _management_apply(self, request, peer_uid):
        if not isinstance(request, dict) or request.get("version") != WIRE_VERSION:
            raise GlobalError("MANAGEMENT_VERSION_REQUIRED")
        action = request.get("action")
        binding = [
            request.get(k)
            for k in (
                "expected_server_instance_id",
                "candidate_generation",
                "authority_epoch",
            )
        ]
        if action == "inspect":
            with self.transaction(binding=binding) as db:
                return self.inspect(db, request.get("agent_id"))
        if action not in {"claim", "recover", "request_status"}:
            raise GlobalError("MANAGEMENT_ACTION_UNSUPPORTED")
        request_id = request.get("request_id")
        if (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 64
            or any(
                c
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for c in request_id
            )
        ):
            raise GlobalError("REQUEST_ID_INVALID")
        with self.transaction(write=action != "request_status", binding=binding) as db:
            # S1 records have a separate protocol table; historical enrollment
            # records retain their original scope and bytes.
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='global_enrollment_requests'"
            ).fetchone()
            if action == "request_status":
                stored = (
                    db.execute(
                        "SELECT receipt FROM global_enrollment_requests WHERE request_id=?",
                        (request_id,),
                    ).fetchone()
                    if exists
                    else None
                )
                if stored is None:
                    raise GlobalError("REQUEST_NOT_FOUND")
                return json.loads(stored[0])
            agent_id = request.get("agent_id")
            expected = request.get("expected_generation")
            token = request.get("new_credential")
            if (
                not integer(agent_id)
                or not integer(expected, 0)
                or not isinstance(token, str)
                or not 20 <= len(token) <= 64
                or any(c.isspace() for c in token)
            ):
                raise GlobalError("ENROLLMENT_INPUT_INVALID")
            request_hash = hashlib.sha256(
                json.dumps(
                    {
                        "action": action,
                        "agent_id": agent_id,
                        "expected_generation": expected,
                        "new_credential_sha256": hashlib.sha256(
                            token.encode()
                        ).hexdigest(),
                        "candidate_generation": self.candidate_generation,
                        "authority_epoch": self.epoch,
                        "server_instance_id": self.instance,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if not exists:
                db.execute(
                    "CREATE TABLE global_enrollment_requests(request_id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,receipt TEXT NOT NULL)"
                )
            prior = db.execute(
                "SELECT request_hash,receipt FROM global_enrollment_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if prior is not None:
                if prior[0] != request_hash:
                    raise GlobalError("REQUEST_CONFLICT")
                return json.loads(prior[1])
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None or row["retired_at"] is not None:
                raise GlobalError("AGENT_UNAVAILABLE")
            if row["credential_generation"] != expected:
                raise GlobalError("CREDENTIAL_GENERATION_CONFLICT")
            if (action == "claim") != (row["registration_token"] is None):
                raise GlobalError("CREDENTIAL_STATE_CONFLICT")
            db.execute(
                "UPDATE agents SET registration_token=?,credential_generation=? WHERE id=?",
                (token, expected + 1, agent_id),
            )
            changed(db)
            receipt = {
                "ok": True,
                "kind": "orrery-global-enrollment-receipt-v1",
                "request_id": request_id,
                "action": action,
                "agent_id": agent_id,
                "name": row["name"],
                "peer_uid": peer_uid,
                "server_instance_id": self.instance,
                "candidate_generation": self.candidate_generation,
                "authority_epoch": self.epoch,
                "old_generation": expected,
                "new_generation": expected + 1,
                "old_fingerprint": fingerprint(row["registration_token"]),
                "new_fingerprint": fingerprint(token),
            }
            db.execute(
                "INSERT INTO global_enrollment_requests VALUES (?,?,?)",
                (request_id, request_hash, json.dumps(receipt, sort_keys=True)),
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS global_enrollment_audit(id INTEGER PRIMARY KEY,request_id TEXT NOT NULL,created_ts TEXT NOT NULL,receipt TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO global_enrollment_audit(request_id,created_ts,receipt) VALUES (?,?,?)",
                (request_id, now(), json.dumps(receipt, sort_keys=True)),
            )
            return receipt

    async def start(self):
        socket = self.paths["management_socket"]
        if socket.exists() or socket.is_symlink():
            raise GlobalError("MANAGEMENT_SOCKET_EXISTS")
        if len(os.fsencode(socket)) > 100:
            raise GlobalError("MANAGEMENT_SOCKET_TOO_LONG")
        self.control = await asyncio.start_unix_server(
            self.handle, path=socket, limit=16385
        )
        socket.chmod(0o600)
        self.socket_inode = socket.stat().st_ino

    async def close(self):
        if self.control is not None:
            self.control.close()
            await self.control.wait_closed()
            for writer in list(self.writers):
                writer.close()
            if self.handlers:
                await asyncio.gather(*list(self.handlers), return_exceptions=True)
            socket = self.paths["management_socket"]
            if socket.exists() and socket.lstat().st_ino == self.socket_inode:
                socket.unlink()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.writers.add(writer)
        try:
            peer_uid = _peer_uid(writer)
            if peer_uid != os.getuid():
                raise GlobalError("PEER_UID_REQUIRED")
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            if not raw.endswith(b"\n") or len(raw) > 16384:
                raise GlobalError("REQUEST_TOO_LARGE")
            value = self.management(json.loads(raw), peer_uid)
        except GlobalError as exc:
            value = {"ok": False, "reason": str(exc)}
        except Exception:
            value = {"ok": False, "reason": "MANAGEMENT_REQUEST_FAILED"}
        try:
            writer.write(json.dumps(value).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            self.handlers.discard(task)
            self.writers.discard(writer)


class S1FastMCP(CompatibilityFastMCP):
    def _publish_or_retain_tool(self, function, tool_name, **kwargs):
        if tool_name not in TOOLS:
            raise GlobalError("S1_TOOL_UNSUPPORTED")
        self._agentstack_declared_tools.add(tool_name)
        self._agentstack_published_tools.add(tool_name)
        # Use the real FastMCP registration while retaining TERM handling and
        # the audited unknown-argument boundary from CompatibilityFastMCP.
        return super(CompatibilityFastMCP, self).tool(function, **kwargs)


def build_global_server(config, *, runtime_class=GlobalRuntime, server_class=S1FastMCP):
    if (
        runtime_class is GlobalRuntime
        and document(config).get("kind") == "orrery-global-server-s2a-v1"
    ):
        from .global_s2a import build_s2a_server

        return build_s2a_server(config)
    runtime = runtime_class(config)

    @asynccontextmanager
    async def lifespan(server):
        await runtime.start()
        try:
            yield
        finally:
            import anyio

            with anyio.CancelScope(shield=True):
                await runtime.close()

    server = server_class(
        name="ORRERY Mail",
        lifespan=lifespan,
        instructions="Isolated global S1 preparation. Only advertised capabilities are supported.",
    )

    @server.tool
    async def health_check() -> dict:
        with runtime.transaction() as db:
            revision = dict(db.execute("SELECT key,value FROM namespace_metadata"))[
                "write_generation"
            ]
        return {
            "status": "ok",
            "mode": "global-preparation",
            "wire_version": WIRE_VERSION,
            "namespace_contract_version": 2,
            "server_instance_id": runtime.instance,
            "candidate_generation": runtime.candidate_generation,
            "authority_epoch": runtime.epoch,
            "mutation_revision": int(revision),
            "activation_enabled": False,
            "supported_tools": sorted(runtime.tools),
            "resources_supported": False,
            **getattr(runtime, "health_fields", {}),
        }

    @server.tool
    async def ensure_project(
        human_key: str | None = None, project_key: str | None = None
    ) -> dict:
        with runtime.transaction():
            return {"namespace": "local", "created": False}

    @server.tool
    async def register_agent(
        expected_server_instance_id: str,
        candidate_generation: str,
        authority_epoch: str,
        program: str,
        model: str,
        agent_id: int | None = None,
        name: str | None = None,
        registration_token: str | None = None,
        task_description: str = "",
        window_row_id: int | None = None,
        window_uuid: str | None = None,
        project_key: str | None = None,
    ) -> dict:
        return runtime.register(
            (expected_server_instance_id, candidate_generation, authority_epoch),
            agent_id,
            name,
            program,
            model,
            task_description,
            registration_token,
            window_row_id,
            window_uuid,
        )

    @server.tool
    async def whois(
        expected_server_instance_id: str,
        candidate_generation: str,
        authority_epoch: str,
        agent_id: int,
        registration_token: str,
        agent_name: str | None = None,
        project_key: str | None = None,
    ) -> dict:
        with runtime.transaction(
            binding=(expected_server_instance_id, candidate_generation, authority_epoch)
        ) as db:
            return runtime.identity(runtime.owner(db, agent_id, registration_token))

    @server.tool
    async def fetch_inbox(
        expected_server_instance_id: str,
        candidate_generation: str,
        authority_epoch: str,
        agent_id: int,
        registration_token: str,
        agent_name: str | None = None,
        project_key: str | None = None,
        limit: int = 20,
        include_bodies: bool = True,
        urgent_only: bool = False,
        since_ts: str | None = None,
    ) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise GlobalError("LIMIT_INVALID")
        if since_ts is not None:
            try:
                cutoff = datetime.fromisoformat(since_ts.replace("Z", "+00:00"))
                if cutoff.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise GlobalError("TIMESTAMP_INVALID") from None
        with runtime.transaction(
            binding=(expected_server_instance_id, candidate_generation, authority_epoch)
        ) as db:
            runtime.owner(db, agent_id, registration_token)
            values = []
            for row in db.execute(
                "SELECT m.*,r.kind,r.read_ts,r.ack_ts,a.name AS sender_name FROM messages m JOIN message_recipients r ON r.message_id=m.id JOIN agents a ON a.id=m.sender_id WHERE r.agent_id=? ORDER BY m.id DESC",
                (agent_id,),
            ):
                value = dict(row)
                if urgent_only and value["importance"] not in {"high", "urgent"}:
                    continue
                if since_ts is not None and instant(value["created_ts"]) <= cutoff:
                    continue
                if not include_bodies:
                    value.pop("body_md", None)
                value["from"] = value.pop("sender_name")
                # Historical scope/label fields are not routing input and
                # should not leak another BCC recipient.
                values.append(value)
                if len(values) >= limit:
                    break
            return values

    def lifecycle(retire):
        async def action(
            expected_server_instance_id: str,
            candidate_generation: str,
            authority_epoch: str,
            agent_id: int,
            registration_token: str,
            agent_name: str | None = None,
            project_key: str | None = None,
        ) -> dict:
            with runtime.transaction(
                write=True,
                binding=(
                    expected_server_instance_id,
                    candidate_generation,
                    authority_epoch,
                ),
            ) as db:
                runtime.owner(db, agent_id, registration_token, retired=True)
                db.execute(
                    "UPDATE agents SET retired_at=? WHERE id=?",
                    (now() if retire else None, agent_id),
                )
                changed(db)
                return {
                    "status": "retired" if retire else "active",
                    "agent_id": agent_id,
                }

        return action

    server.tool(lifecycle(True), name="retire_agent")
    server.tool(lifecycle(False), name="unretire_agent")
    return server
