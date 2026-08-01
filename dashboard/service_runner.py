#!/usr/bin/env python3
"""Run the dashboard with bounded persistent logs and crash diagnostics."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import TextIO


HERE = pathlib.Path(__file__).resolve().parent
RUNTIME_DIR = pathlib.Path(
    os.environ.get("AGENTSTACK_RUNTIME_DIR", pathlib.Path.home() / ".agentstack" / "runtime")
).expanduser()
LOG_PATH = pathlib.Path(
    os.environ.get("AGENTSTACK_DASHBOARD_LOG", RUNTIME_DIR / "dashboard.log")
).expanduser()
STATE_PATH = pathlib.Path(
    os.environ.get("AGENTSTACK_DASHBOARD_RUN_STATE", RUNTIME_DIR / "dashboard-service.json")
).expanduser()


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


LOG_MAX_BYTES = _env_int("AGENTSTACK_DASHBOARD_LOG_MAX_BYTES", 5 * 1024 * 1024, 1024)
LOG_BACKUPS = _env_int("AGENTSTACK_DASHBOARD_LOG_BACKUPS", 3, 1)
RESTART_DELAY = _env_int("AGENTSTACK_DASHBOARD_RESTART_DELAY", 5, 0)
SELF_RESTART = os.environ.get("AGENTSTACK_DASHBOARD_SELF_RESTART", "").lower() in {
    "1", "true", "yes", "on",
}


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


def _configure_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(UTCFormatter("%(asctime)sZ %(levelname)s %(message)s"))
    logger = logging.getLogger("agentstack.dashboard.service")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        LOG_PATH.chmod(0o600)
    except OSError:
        pass
    return logger


def _read_previous_state() -> dict[str, object] | None:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _write_state(server_path: pathlib.Path) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "supervisor_pid": os.getpid(),
        "server_path": str(server_path),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    temporary = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, STATE_PATH)


def _remove_state() -> None:
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def _describe_returncode(returncode: int) -> str:
    if returncode >= 0:
        return f"exit_code={returncode}"
    number = -returncode
    try:
        name = signal.Signals(number).name
    except ValueError:
        name = "UNKNOWN"
    return f"signal={name}({number})"


def _forward_output(stream: TextIO, logger: logging.Logger) -> None:
    for line in stream:
        logger.info("server | %s", line.rstrip("\r\n"))


def run(server_path: pathlib.Path) -> int:
    logger = _configure_logger()
    previous = _read_previous_state()
    if previous:
        logger.warning("unclean supervisor exit detected; previous_state=%s", previous)
    _write_state(server_path)

    child: subprocess.Popen[str] | None = None
    watchdog_write_fd: int | None = None
    stopping_signal: int | None = None

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stopping_signal
        stopping_signal = signum
        logger.info("supervisor received signal=%s(%d); forwarding to server",
                    signal.Signals(signum).name, signum)
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        while True:
            if stopping_signal is not None:
                logger.info("dashboard supervisor stopped before next restart")
                return 0
            logger.info(
                "starting dashboard server supervisor_pid=%d port=%s path=%s",
                os.getpid(), os.environ.get("AGENTSTACK_PORT", "8770"), server_path,
            )
            watchdog_read_fd, watchdog_write_fd = os.pipe()
            child_env = os.environ.copy()
            child_env["AGENTSTACK_DASHBOARD_SUPERVISOR_FD"] = str(watchdog_read_fd)
            child = subprocess.Popen(
                [sys.executable, "-u", str(server_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=child_env,
                pass_fds=(watchdog_read_fd,),
            )
            os.close(watchdog_read_fd)
            logger.info("dashboard server started child_pid=%d", child.pid)
            assert child.stdout is not None
            _forward_output(child.stdout, logger)
            returncode = child.wait()
            os.close(watchdog_write_fd)
            watchdog_write_fd = None
            log_exit = logger.info if stopping_signal is not None else logger.error
            log_exit(
                "dashboard server exited child_pid=%d %s",
                child.pid, _describe_returncode(returncode),
            )
            child = None

            if stopping_signal is not None:
                logger.info("dashboard supervisor stopped after requested signal")
                return 0
            if not SELF_RESTART:
                logger.info("leaving restart to the service manager")
                return 1
            logger.warning("restarting dashboard server in %d seconds", RESTART_DELAY)
            if RESTART_DELAY:
                time.sleep(RESTART_DELAY)
    except Exception:
        logger.exception("dashboard service runner crashed")
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        return 1
    finally:
        if watchdog_write_fd is not None:
            os.close(watchdog_write_fd)
        _remove_state()


def main() -> int:
    server_path = pathlib.Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else HERE / "server.py"
    return run(server_path)


if __name__ == "__main__":
    raise SystemExit(main())
