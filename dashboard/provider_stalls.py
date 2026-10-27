"""Legacy tmux provider-stop observation; never resumes or types into agents."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import uuid

CAPACITY = "■ Selected model is at capacity. Please try a different model."
# Codex 0.159.2: compact_remote_v2.rs and session/turn.rs pass this one
# prefix to CodexErr::to_error_event; local compact and regular turns pass None.
# Codex PreWrap writes hard newlines that capture-pane -J cannot join. Accept
# one newline at each word boundary of these fixed literals, never blank lines.
_ERROR_GAP = r"(?:[ \t]+|[ \t]*\n[ \t]*)"
_ERROR = re.compile(
    r"(?m)^[ \t]*■ (?:"
    + re.escape("Error running remote compact task:").replace(r"\ ", _ERROR_GAP)
    + _ERROR_GAP + r")?"
    + re.escape(CAPACITY.removeprefix("■ ")).replace(r"\ ", _ERROR_GAP)
    + r"[ \t]*$"
)
try:
    from dashboard.terminal_state import _ANSI, _STALL_WORK as _WORK
except ModuleNotFoundError:
    from terminal_state import _ANSI, _STALL_WORK as _WORK

# Newer Codex shows the display name ("GPT-6.1 Terra high"), older ones the ID.
_FOOTER = re.compile(r"^(?:gpt-\S+(?:\s+(?:sol|luna|astra|terra))?|sol|luna|astra|terra)"
                     r"\s+(?:low|medium|high|xhigh|max|ultra)\b.*$", re.I)
_UI = re.compile(r"^(?:[─━\s]+|\? for shortcuts.*|\d+% context left.*)$")
# Codex rust-v0.159.2, tui/src/bottom_pane/unified_exec_footer.rs::summary_text.
# Only this complete footer may sit between the error and the composer.
_BACKGROUND_TERMINALS = re.compile(
    r"(?:1 background terminal|(?:[2-9]|[1-9][0-9]+) background terminals)"
    r" running · /ps to view · /stop to close"
)


def classify(text: str) -> dict:
    """An exact terminal error and known composer chrome identify a current stop."""
    text = _ANSI.sub("", text).replace("\r", "")
    errors = list(_ERROR.finditer(text))
    if errors:
        suffix = [line.strip() for line in text[errors[-1].end():].splitlines() if line.strip()]
        prompts = [i for i, line in enumerate(suffix) if line.startswith("›")]
        prompt = prompts[0] if len(prompts) == 1 else -1
        if (
            prompt in (0, 1)
            and (prompt == 0 or _BACKGROUND_TERMINALS.fullmatch(suffix[0]))
            and all(
                _FOOTER.fullmatch(line) or _UI.fullmatch(line)
                for line in suffix[prompt + 1:]
            )
        ):
            return {"state": "capacity", "error": CAPACITY}
    # Historical work before a terminal error cannot rearm a stopped episode.
    tail = text[errors[-1].end():] if errors else "\n".join(text.splitlines()[-16:])
    if _WORK.search(tail):
        return {"state": "work"}
    return {"state": "idle" if re.search(r"(?m)^\s*›", tail) else "unknown"}


def observe(text: str) -> dict:
    """Keep bounded, content-free history anchors alongside a current stop.

    Composer/footer text is excluded. Only a standalone submitted user block
    (including indented hard wraps) is an input anchor. Ambiguous or fenced
    history may still classify as capacity, but cannot prove a retry.
    """
    result = classify(text)
    if result["state"] != "capacity":
        return result
    text = _ANSI.sub("", text).replace("\r", "")
    errors = list(_ERROR.finditer(text))
    prefix = text[:errors[-1].end()]
    history = []
    # A capture can start inside a code fence: do not guess its quoting state.
    if len(prefix) <= 100_000 and not re.search(r"(?m)^[ \t]*(?:```|~~~)", prefix):
        position = 0
        for error in errors:
            for block in re.split(r"\n[ \t]*\n+", text[position:error.start()]):
                if not block.strip():
                    continue
                lines = block.strip("\n").splitlines()
                user = (re.match(r"^›[ \t]+\S", lines[0]) is not None
                        and all(re.match(r"^[ \t]{2,}\S", line) for line in lines[1:]))
                normalized = " ".join(block.split())
                history.append(("u:" if user else "o:") + hashlib.sha256(normalized.encode()).hexdigest())
            normalized = " ".join(error.group().split())
            history.append("e:" + hashlib.sha256(normalized.encode()).hexdigest())
            position = error.end()
    result["history"] = history[-64:]
    return result


def _history_append(previous, current):
    """Return a proven append, or None when old history cannot be located.

    An error alone is never an anchor. A repeated longest overlap is ambiguous;
    selecting its first/last occurrence could turn a redraw into another send.
    """
    if previous == current:
        return []
    for size in range(min(len(previous), len(current)), 1, -1):
        suffix = previous[-size:]
        if not any(not token.startswith("e:") for token in suffix):
            continue
        matches = [index for index in range(len(current) - size + 1)
                   if current[index:index + size] == suffix]
        if len(matches) > 1:
            return None
        if matches:
            return current[matches[0] + size:]
    return None


def _submitted_retry(appended):
    # Errors without submitted inputs, output, queued-input chrome and quoted
    # prompts cannot consume a new boundary. Several fast retries coalesce into
    # the latest observed pair; only that pair can be confirmed next capture.
    return (bool(appended) and len(appended) % 2 == 0
            and all(token.startswith("u:" if index % 2 == 0 else "e:")
                    for index, token in enumerate(appended)))


class Monitor:
    """A durable event claim bounds each episode to one Mail send attempt."""

    def __init__(self, path: Path, send, *, session_names=None):
        self.path = Path(path)
        self.send = send
        self._lock = threading.RLock()
        self._rows = {}
        self._views = {}
        self._saved_text = None
        self._session_names = session_names

    @contextmanager
    def _transaction(self):
        import fcntl  # The worker is legacy POSIX-only; keep global/Windows imports safe.
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("unsafe provider-stall lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                state_fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                self._rows = {}
                self._saved_text = None
            else:
                with os.fdopen(state_fd, encoding="utf-8") as source:
                    info = os.fstat(source.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 2_000_000):
                        raise ValueError("unsafe provider-stall state")
                    saved = json.load(source)
                if saved.get("version") != 1 or not isinstance(saved.get("events"), dict):
                    raise ValueError("invalid provider-stall state")
                self._rows = saved["events"]
                self._saved_text = json.dumps(saved, ensure_ascii=False, sort_keys=True)
            yield
        finally:
            os.close(fd)

    def _save(self):
        text = json.dumps({"version": 1, "events": self._rows}, ensure_ascii=False, sort_keys=True)
        # Publish even when another monitor saved the same state in between.
        self._views = {
            (row["name"], row["generation"]): {
                key: row[key] for key in ("kind", "since", "error", "notification_status")
            } for row in self._rows.values() if row["phase"] == "stalled"
        }
        if text == self._saved_text:
            return
        fd, name = tempfile.mkstemp(prefix=".provider-stalls-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                target.write(text)
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
                self._saved_text = text
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def tick(self, samples, *, now=None):
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        samples = list(samples)
        # No capacity candidate and no journal: do not even create a lock/root.
        if not os.path.lexists(self.path) and not any(
            row["evidence"].get("state") == "capacity" for row in samples
        ):
            return
        with self._lock, self._transaction():
            for row in samples:
                key = json.dumps([row["project_key"], row["agent_id"], row["generation"]])
                old = self._rows.get(key)
                capture = row["capture_id"]
                # A delayed scrape must not undo or confirm a newer transition.
                if old and capture <= old["capture_id"]:
                    continue
                if old and now - old["last_seen"] >= 3600:
                    old["last_seen"] = now
                state = row["evidence"].get("state", "unknown")
                if state == "work":
                    if old:
                        self._rows[key] = dict(name=row["name"], generation=row["generation"],
                            phase="clear", capture_id=capture, last_seen=now)
                    continue
                if state != "capacity":
                    if old and "retry_capture" in old:
                        old.pop("retry_capture")
                        old.pop("retry_since")
                        old["capture_id"] = capture
                    if old and old["phase"] == "candidate":
                        old.update(phase="clear", capture_id=capture)
                    continue
                history = row["evidence"].get("history", [])
                if old is None or old["phase"] == "clear":
                    self._rows[key] = dict(name=row["name"], generation=row["generation"],
                        phase="candidate", capture_id=capture, first_seen=now,
                        last_seen=now, history=history)
                    continue
                if old["phase"] == "stalled":
                    previous = old.get("history", [])
                    if history and history == previous and "retry_capture" in old:
                        old["first_seen"] = old.pop("retry_since")
                        old.pop("retry_capture")
                        # Same submitted-input/error boundary, distinct capture:
                        # proceed to the existing durable claim and single send.
                    else:
                        had_pending = old.pop("retry_capture", None) is not None
                        old.pop("retry_since", None)
                        if had_pending:
                            old["capture_id"] = capture
                        appended = _history_append(previous, history) if previous and history else None
                        if history and (not previous or appended):
                            # Old journals bootstrap without announcing a retry.
                            # A proven append is consumed even if not an input,
                            # so it cannot become a new boundary after restart.
                            old.update(history=history, capture_id=capture)
                        if _submitted_retry(appended):
                            old.update(retry_capture=capture, retry_since=now)
                        continue
                elif history != old.get("history", []):
                    # A different stop cannot confirm the first candidate.
                    old.update(history=history, capture_id=capture, first_seen=now)
                    continue
                old.update(phase="stalled", capture_id=capture, kind="model_capacity", error=CAPACITY,
                           event_id=str(uuid.uuid4()), since=now,
                           notification_status="delivery_unknown")
                # Commit the claim before a call whose result could be lost.
                self._save()
                if not row.get("parent"):
                    old["notification_status"] = "no_parent"
                else:
                    try:
                        result = self.send(row, dict(old))
                        old["notification_status"] = result if result in {
                            "sent", "failed", "parent_unverified", "delivery_unknown"
                        } else "delivery_unknown"
                    except Exception:
                        old["notification_status"] = "delivery_unknown"
                        logging.warning("Provider-stop delivery result is unknown for %s", row["name"])
            # A replacement generation is positive evidence. Mere absence or a
            # long dashboard outage must never rearm the same stopped session.
            current = {(row["project_key"], row["agent_id"]): row["generation"] for row in samples}
            # Only query definitive tmux inventory for rows past the expiry.
            expired = any(now - row["last_seen"] >= 30 * 86400 for row in self._rows.values())
            names = self._session_names() if expired and self._session_names else None
            for key, value in list(self._rows.items()):
                project, agent_id, generation = json.loads(key)
                replacement = current.get((project, agent_id), generation)
                age = now - value["last_seen"]
                if ((age >= 7 * 86400 and replacement != generation)
                        or (age >= 30 * 86400 and names is not None and value["name"] not in names)):
                    del self._rows[key]
            self._save()

    def view(self, name, generation):
        row = self._views.get((name, generation))
        return dict(row) if row else None

    def run(self, sample, stop, *, interval=5):
        while not stop.is_set():
            try:
                self.tick(sample())
            except Exception:
                logging.exception("Provider-stop observation failed")
            stop.wait(interval)


def message(event: dict) -> str:
    when = datetime.fromtimestamp(event["since"], timezone(timedelta(hours=9))).isoformat(timespec="seconds")
    return ("telemetry の自動検知\n\n"
            f"検知時刻: {when} (JST)\n"
            f"{event['error']}\n\n"
            f"Event: {event['event_id']}\n自動再開はしていません。")
