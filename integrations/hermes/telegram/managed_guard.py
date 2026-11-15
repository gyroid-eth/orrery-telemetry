"""Managed Telegram lifecycle. Contains no credentials or Hermes core changes."""
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time

LOG = logging.getLogger("hermes.telegram.managed")


def install_network_log_redaction():
    """Redact before any handler sees HTTPX/httpcore messages, including child loggers.

    Match URL paths rather than storing tokens in a process-global registry. The
    factory lives across plugin reloads; installing a second copy does not wrap it.
    """
    factory = logging.getLogRecordFactory()
    if getattr(factory, "_hermes_telegram_redaction", False):
        return

    def redacted_record(*args, **kwargs):
        record = factory(*args, **kwargs)
        if record.name == "httpx" or record.name.startswith(("httpx.", "httpcore")):
            record.msg = re.sub(r"(/bot)[^/\s\"'?]+", r"\1<redacted>", record.getMessage())
            record.args = ()
            # Transport exception tracebacks can contain the same credential URL.
            if record.exc_info:
                if record.exc_info[0] is not None:
                    record.msg += " [" + record.exc_info[0].__name__ + "]"
                record.exc_info = None
                record.exc_text = None
        return record

    redacted_record._hermes_telegram_redaction = True
    logging.setLogRecordFactory(redacted_record)


def is_tui_backend():
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    return getattr(spec, "name", None) == "tui_gateway.entry"


def _write_private_log(home, filename, data):
    if not home:
        raise RuntimeError("managed Telegram requires HERMES_HOME")
    directory = Path(home) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".telegram-status-", dir=str(directory))
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.write("\n")
        os.replace(name, directory / filename)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_status(state, home=None, **details):
    data = dict(state=state, pid=os.getpid(), updated_at=time.time(),
                run_id=os.environ.get("HERMES_TELEGRAM_RUN_ID", ""), **details)
    _write_private_log(home or os.environ.get("HERMES_HOME"), "telegram-status.json", data)


def write_credential_diagnostic(home, reason, returncode=None):
    # A separate, owner-only last-failure record. Never accept arbitrary error
    # text, reference fields, helper output, or an environment dump.
    if reason not in {"configuration-invalid", "reference-invalid", "unsupported-store",
                      "security-nonzero", "security-timeout", "security-unavailable",
                      "empty-or-invalid-value", "secret-service-timeout", "secret-service-unavailable"}:
        raise ValueError("invalid_telegram_diagnostic")
    data = dict(reason=reason, pid=os.getpid(), updated_at=time.time())
    if reason == "security-nonzero" and type(returncode) is int:
        data["returncode"] = returncode
    _write_private_log(home, "telegram-credential-diagnostic.json", data)


class PollOwner:
    def __init__(self, home):
        self.home = Path(home)
        self.lock = None
        self.username = None
        self.state = None

    def status(self, state, **details):
        self.state = state
        write_status(state, home=self.home, **details)

    def acquire(self):
        if self.lock is not None:
            return False
        (self.home / "logs").mkdir(parents=True, exist_ok=True)
        self.lock = (self.home / "logs" / "telegram-poller.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            LOG.error("Telegram poller already owns this profile; refusing duplicate pid=%s",
                      os.getpid())
            # Do not overwrite the real owner's status from the losing process.
            return False
        self.status("telegram-starting")
        return True

    def release(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def ready(self, username):
        # Telegram usernames are public and have a small, fixed alphabet.
        username = str(username or "")
        if not username or not all(c.isascii() and (c.isalnum() or c == "_") for c in username):
            raise RuntimeError("Telegram getMe returned no valid public username")
        LOG.info("Telegram ready profile=%s username=@%s poller_pid=%s",
                 self.home.name, username, os.getpid())
        self.username = username
        self.status("telegram-ready", username=username)

    def healthy(self):
        if self.username and self.state != "telegram-ready":
            self.status("telegram-ready", username=self.username)

    def error(self, code):
        self.status("telegram-conflict" if code == 409 else "telegram-error",
                    error_code=str(code) if isinstance(code, int) else "unknown")

