"""Bounded, secret-free command-backed auth core. No launcher or deployment.

The standalone runner reads an existing approved intent owner; it never writes
one, registers an agent, changes trust, or puts credentials in an environment.
Only stdlib imports: the deployed runner can be a single reviewed source file.
"""
from __future__ import annotations

import sys

# The executable must be deployed with an approved absolute Python -I
# shebang. Refuse a manually overridden non-isolated interpreter before
# importing non-builtin modules or fetching any credential.
if __name__ == "__main__" and not sys.flags.isolated:
    sys.stderr.write("LOCAL_MODEL_AUTH_FAILED\n")
    raise SystemExit(1)

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time
import unicodedata

CONTRACT_VERSION = 1
AUTH_FAILED = "LOCAL_MODEL_AUTH_FAILED"


class LocalModelError(RuntimeError):
    """Public errors contain a fixed reason, never command output or paths."""


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def plain_text(value):
    return isinstance(value, str) and bool(value) and not any(
        unicodedata.category(char).startswith("C") for char in value)


def private_directory(path, expected_uid, *, exact_mode=None):
    """Walk with dirfds and NOFOLLOW, allowing only root-owned sticky /tmp.

    Caller owns the returned descriptor. Never resolve a symlink and then
    accidentally validate the substituted target instead of the requested path.
    """
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            writable = stat.S_IMODE(info.st_mode) & 0o022
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in (0, expected_uid) or (writable and not sticky_root):
                raise ValueError("unsafe ancestor")
        info = os.fstat(fd)
        if exact_mode is not None and (info.st_uid != expected_uid or
                                      stat.S_IMODE(info.st_mode) != exact_mode):
            raise ValueError("unsafe directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def secure_read(path, expected_uid, *, mode=None, parent_mode=None, limit=1048576):
    path = Path(path)
    directory = private_directory(path.parent, expected_uid, exact_mode=parent_mode)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                    before.st_uid != expected_uid or before.st_size > limit or
                    stat.S_IMODE(before.st_mode) & 0o022 or
                    (mode is not None and stat.S_IMODE(before.st_mode) != mode)):
                raise ValueError("unsafe file")
            chunks, size = [], 0
            while chunk := os.read(fd, min(65536, limit + 1 - size)):
                size += len(chunk)
                if size > limit:
                    raise ValueError("oversized file")
                chunks.append(chunk)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("file changed during read")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def validate_auth_context(plan):
    if not isinstance(plan, dict):
        raise LocalModelError(AUTH_FAILED)
    env = plan.get("env")
    if (not isinstance(env, dict) or set(env) != {"PATH", "LANG", "HOME", "USER", "LOGNAME"} or
            env["PATH"] != "/usr/bin:/bin" or env["LANG"] != "C" or
            not all(plain_text(value) for value in env.values()) or
            not Path(env["HOME"]).is_absolute() or not plain_text(plan.get("cwd")) or
            not Path(plan["cwd"]).is_absolute() or
            ".." in Path(env["HOME"]).parts or ".." in Path(plan["cwd"]).parts or
            any(type(plan.get(key)) is not int for key in
                ("timeout_ms", "stdout_limit_bytes", "refresh_interval_ms")) or
            plan.get("timeout_ms") != 5000 or plan.get("stdout_limit_bytes") != 8192 or
            plan.get("refresh_interval_ms") != 300000):
        raise LocalModelError(AUTH_FAILED)


def validate_runner(plan, *, observed_digest, observed_contract_version):
    if (type(observed_contract_version) is not int or type(plan.get("runner_contract_version")) is not int or
            observed_contract_version != CONTRACT_VERSION or
            plan.get("runner_contract_version") != observed_contract_version or
            plan.get("runner_digest") != observed_digest):
        raise LocalModelError(AUTH_FAILED)


def check_runner_resources(runner, expected_uid):
    try:
        if (not isinstance(runner, dict) or not isinstance(runner.get("dependency_digests"), dict) or
                not re.fullmatch(r"[0-9a-f]{64}", runner.get("sha256", ""))):
            raise ValueError("invalid runner reference")
        data = secure_read(runner["path"], expected_uid, mode=0o500, parent_mode=0o700)
        validate_runner({"runner_digest": runner["sha256"],
                         "runner_contract_version": runner["contract_version"]},
                        observed_digest=hashlib.sha256(data).hexdigest(),
                        observed_contract_version=CONTRACT_VERSION)
        for path, digest in runner["dependency_digests"].items():
            if not isinstance(digest, str) or hashlib.sha256(secure_read(path, expected_uid)).hexdigest() != digest:
                raise ValueError("dependency changed")
    except (KeyError, TypeError, OSError, ValueError):
        raise LocalModelError(AUTH_FAILED) from None


def _bounded_process(argv, *, cwd, env, timeout, **_):
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(argv, cwd=cwd, env=env, shell=False, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    complete = False
    try:
        chunks, size = [], 0
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not ready.select(remaining):
                    raise ValueError("credential deadline")
                chunk = os.read(process.stdout.fileno(), 8193 - size)
                if not chunk:
                    ready.unregister(process.stdout)
                    break
                size += len(chunk)
                if size > 8192:
                    raise ValueError("credential output limit")
                chunks.append(chunk)
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        complete = True
        return subprocess.CompletedProcess(argv, code, stdout=b"".join(chunks))
    finally:
        # Also terminate descendants that retained the stdout pipe on timeout.
        if not complete:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.stdout.close()
        process.wait()


def run_auth(plan, *, executor=None):
    try:
        validate_auth_context(plan)
        argv = plan["argv"]
        if (not isinstance(argv, list) or not argv or
                not all(plain_text(arg) for arg in argv) or not Path(argv[0]).is_absolute()):
            raise ValueError("invalid command")
        result = (executor or _bounded_process)(list(argv), cwd=plan["cwd"], env=dict(plan["env"]),
                                               shell=False, timeout=5, stdout=subprocess.PIPE,
                                               stderr=subprocess.DEVNULL)
        raw = result.stdout
        if result.returncode != 0 or not isinstance(raw, bytes) or not 0 < len(raw) <= 8192:
            raise ValueError("invalid credential result")
        token = raw[:-1] if raw.endswith(b"\n") else raw
        text = token.decode("utf-8")
        if not plain_text(text) or not text.strip():
            raise ValueError("invalid credential")
        return token
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, LocalModelError):
        raise LocalModelError(AUTH_FAILED) from None


def intent_digest(intent):
    return canonical_digest({key: value for key, value in intent.items() if key != "intent_digest"})


def read_auth_plan(owner, digest, provider, *, runner_path):
    """Read an existing owner, not a new auth cache or second intent store."""
    try:
        intent = json.loads(secure_read(owner, os.getuid(), mode=0o600, parent_mode=0o700))["launch_intent"]
        if not isinstance(intent, dict) or not isinstance(intent.get("scope_evidence"), dict):
            raise ValueError("invalid intent")
        if intent_digest(intent) != digest or intent["intent_digest"] != digest or intent["provider"] != provider:
            raise ValueError("intent mismatch")
        resolved = intent["model"]["resolved"]
        auth = resolved["auth"]
        if (intent["operator_uid"] != os.getuid() or auth["context"]["cwd"] != intent["local_models_root"]):
            raise ValueError("auth context mismatch")
        if Path(auth["runner"]["path"]) != Path(runner_path).absolute():
            raise ValueError("runner mismatch")
        check_runner_resources(auth["runner"], os.getuid())
        # Profile drift is checked here too, without importing executable
        # dependencies before validating their digest.
        import tomllib
        root = auth["context"]["cwd"]
        ref = intent["model"]["ref"]
        if not isinstance(ref, str) or not re.fullmatch(r"local:[a-z][a-z0-9_-]{0,47}", ref):
            raise ValueError("invalid profile reference")
        name = ref[6:]
        profile = tomllib.loads(secure_read(Path(root) / (name + ".toml"), os.getuid(),
                                          mode=0o600, parent_mode=0o700, limit=65536).decode())
        if (canonical_digest(profile) != intent["model"]["profile_sha256"] or
                profile[provider] != resolved["adapter"]):
            raise ValueError("profile changed")
        for path, digest in intent.get("scope_evidence", {}).items():
            if hashlib.sha256(secure_read(path, os.getuid())).hexdigest() != digest:
                raise ValueError("settings changed")
        plan = {**auth["context"], "argv": resolved["adapter"]["auth_command"]}
        validate_auth_context(plan)
        return plan
    except (KeyError, TypeError, ValueError, OSError):
        raise LocalModelError(AUTH_FAILED) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read an approved local model credential")
    parser.add_argument("--intent", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--provider", required=True, choices=("codex", "claude"))
    args = parser.parse_args(argv)
    try:
        plan = read_auth_plan(args.intent, args.digest, args.provider, runner_path=__file__)
        token = run_auth(plan)
    except LocalModelError:
        sys.stderr.write(AUTH_FAILED + "\n")
        return 1
    sys.stdout.buffer.write(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
