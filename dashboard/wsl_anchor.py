#!/usr/bin/env python3
"""Keep the WSL distro running while ORRERY has work, and only then.

WSL stops a distro about 15 s after its last Windows-side client (wsl.exe, a
Windows Terminal tab) exits, whatever runs inside it: closing every Ubuntu
window stopped tmux, the dashboard and every agent (WSL 2.7.13, 2026-09-29).
Setting `[general] instanceIdleTimeout=-1` in the Windows .wslconfig prevents
that for every distro, forever, so the user has to `wsl --shutdown` by hand.

Instead, while there is work, the dashboard keeps one hidden Windows `wsl.exe`
running for this distro. That process is a client WSL counts; it runs the
`hold` loop below (the anchor) in the foreground and ends by itself when the
work is gone, after which WSL's default idle stop applies again. Nothing here
runs `wsl --shutdown` / `--terminate` or changes .wslconfig. Checked on the
real machine: a hidden `wsl.exe` started through interop with
`Start-Process` outlives its launcher and holds the distro until it exits.

Work, for the 10/1 scope:
- a live agent process (claude / codex / antigravity) in a tmux session,
  including one waiting for approval or a reply: only its exit ends it;
- a launch reservation under `<state>/reservations/`, made before a launch
  starts (dashboard NEW AGENT, agent-start) and removed when it settles, or
  expired after RESERVATION_SECONDS;
- a visible ORRERY page (the dashboard, also inside the cockpit): the page
  renews a lease under `<state>/leases/` while it is shown and drops it when
  it is closed. A tmux control-mode client is not a viewer: the cockpit keeps
  a recorder attached to every session whether or not anyone looks.
The dashboard, Mail, the watcher and the anchor itself are never work, or the
distro would never stop.

When the work cannot be observed (tmux or ps fails, a file cannot be read),
the last confirmed hold stands and the header says the state is unknown:
losing live work is worse than keeping WSL up a little longer.

Files under the state directory (`$AGENTSTACK_RUNTIME_DIR/wsl-anchor`):
- `lock`        held (flock) by the running anchor for its whole life
- `anchor.json` written by the anchor once it holds the lock (ready)
- `wanted.json` written by the controller every tick: hold, lease end, pids
- `status.json` the controller's view, for the header and doctor
- `work.lock`   serialises a reservation against the controller's decision
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

TICK_SECONDS = 5.0
# Nothing to hold for this long before letting the anchor go.
GRACE_SECONDS = 60.0
# How long a controller's hold stays valid without being renewed. If the
# dashboard dies, the anchor still holds while the recorded agents live.
LEASE_SECONDS = 90.0
READY_TIMEOUT_SECONDS = 20.0
# A launch reservation that nobody removed (the launcher died) stops counting.
RESERVATION_SECONDS = 180.0
# A page lease is renewed every ~15 s while the page is visible.
PAGE_LEASE_SECONDS = 45.0
MAX_START_FAILURES = 3
RETRY_AFTER_FAILURE_SECONDS = 300.0
ANCHOR_POLL_SECONDS = 2.0

_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./:@+,=-]+$")


def enabled() -> bool:
    return os.environ.get("AGENTSTACK_WSL_ANCHOR", "1").strip() != "0"


def state_dir() -> pathlib.Path:
    runtime = os.environ.get("AGENTSTACK_RUNTIME_DIR") or os.path.join(
        os.environ.get("AGENTSTACK_HOME") or os.path.expanduser("~/.agentstack"), "runtime"
    )
    return pathlib.Path(runtime) / "wsl-anchor"


def _read_json(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def process_start_ticks(pid: int) -> int | None:
    """Start time of a Linux process in clock ticks since boot (field 22)."""
    try:
        raw = pathlib.Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    try:
        return int(raw[raw.rindex(")") + 2:].split()[19])
    except (ValueError, IndexError):
        return None


def process_state(pid: int, start_ticks: int | None) -> str:
    """`alive`, `dead` (gone, or the PID now belongs to another process) or
    `unknown` (/proc could not be read for another reason)."""
    try:
        raw = pathlib.Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError):
        return "dead"
    except (OSError, ValueError):
        return "unknown"
    try:
        current = int(raw[raw.rindex(")") + 2:].split()[19])
    except (ValueError, IndexError):
        return "unknown"
    if start_ticks is not None and current != start_ticks:
        return "dead"
    return "alive"


def process_matches(pid: int, start_ticks: int | None) -> bool:
    """The same process is still alive: a recycled PID has another start time."""
    return process_state(pid, start_ticks) == "alive"


def _read_json_checked(path: pathlib.Path) -> dict | None:
    """The JSON object in path, or None when it cannot be read or parsed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _locked(directory: pathlib.Path):
    """Exclusive flock on work.lock, as a context manager."""
    import contextlib

    @contextlib.contextmanager
    def guard():
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory / "work.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    return guard()


_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def reserve(directory: pathlib.Path, token: str) -> None:
    """Record a launch that is about to start. Taken under work.lock, so a
    controller deciding to release either sees it or has already released."""
    if not _TOKEN.match(token):
        raise ValueError(f"bad reservation token {token!r}")
    with _locked(directory):
        (directory / "reservations").mkdir(parents=True, exist_ok=True)
        (directory / "reservations" / token).touch()


def release_reservation(directory: pathlib.Path, token: str) -> None:
    if not _TOKEN.match(token):
        return
    try:
        (directory / "reservations" / token).unlink()
    except OSError:
        pass


def _fresh(folder: pathlib.Path, ttl: float, now: float) -> int:
    count = 0
    try:
        entries = list(folder.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if now - entry.stat().st_mtime < ttl:
                count += 1
        except OSError:
            continue
    return count


def touch_page_lease(directory: pathlib.Path, token: str, release: bool = False) -> bool:
    if not _TOKEN.match(token):
        return False
    folder = directory / "leases"
    if release:
        try:
            (folder / token).unlink()
        except OSError:
            pass
        return True
    folder.mkdir(parents=True, exist_ok=True)
    (folder / token).touch()
    return True


def anchor_running(directory: pathlib.Path) -> bool:
    """True while some anchor holds the lock. Never blocks."""
    lock = directory / "lock"
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# The anchor: runs inside the hidden Windows wsl.exe, in the foreground.
# --------------------------------------------------------------------------- #
def keep_holding(wanted: dict, now: float) -> bool:
    if wanted.get("hold") and float(wanted.get("until") or 0) > now:
        return True
    # The controller is gone (or said release) but agents it saw may live on:
    # an expired lease never ends work that is still running.
    for entry in wanted.get("pids") or []:
        try:
            pid, ticks = int(entry[0]), entry[1]
        except (TypeError, ValueError, IndexError):
            continue
        if process_state(pid, ticks if isinstance(ticks, int) else None) != "dead":
            return True  # alive, or cannot tell: never end work we cannot see
    return False


def hold(directory: pathlib.Path, nonce: str, poll: float = ANCHOR_POLL_SECONDS) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return 0  # another anchor already holds this distro
    ready = directory / "anchor.json"
    _write_json(ready, {
        "nonce": nonce,
        "pid": os.getpid(),
        "start_ticks": process_start_ticks(os.getpid()),
        "started": time.time(),
    })
    # Until the controller's wishes can be read, hold for one lease.
    last_good: dict = {"hold": True, "until": time.time() + LEASE_SECONDS, "pids": []}
    try:
        while True:
            wanted = _read_json_checked(directory / "wanted.json")
            if wanted is not None:
                last_good = wanted
            # An unreadable file keeps the last wishes; their lease still ends.
            if not keep_holding(last_good, time.time()):
                break
            time.sleep(poll)
    finally:
        if _read_json(ready).get("nonce") == nonce:
            try:
                ready.unlink()
            except OSError:
                pass
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return 0


# --------------------------------------------------------------------------- #
# Starting the Windows client (from inside WSL, through interop).
# --------------------------------------------------------------------------- #
def _powershell() -> str | None:
    found = shutil.which("powershell.exe")
    if found:
        return found
    for root in ("/mnt/c", "/c"):
        candidate = f"{root}/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def interop_env(run_dir: str = "/run/WSL") -> dict:
    """Environment whose WSL_INTEROP points at a live interop socket.

    A background process inherits WSL_INTEROP from the shell that started it,
    and that socket goes away with the shell's window. Running a Windows
    program then fails, although other sessions (the anchor's own among them)
    still have working sockets. Prefer the inherited one while it exists,
    else the newest one present.
    """
    env = dict(os.environ)
    current = env.get("WSL_INTEROP", "")
    if current and os.path.exists(current):
        return env
    try:
        candidates = [os.path.join(run_dir, name) for name in os.listdir(run_dir) if name.endswith("_interop")]
    except OSError:
        return env
    live = [path for path in candidates if os.path.exists(path)]
    if live:
        env["WSL_INTEROP"] = max(live, key=lambda path: os.stat(path).st_mtime)
    return env


def start_script(distro: str, argv: list[str]) -> str:
    """PowerShell that starts a hidden wsl.exe running argv and prints its PID.

    Start-Process joins ArgumentList with spaces and does not quote, so every
    argument must be a plain token (checked by the caller). The distro name is
    passed through base64 so no quoting layer can change it.
    """
    payload = base64.b64encode(json.dumps([distro, *argv]).encode("utf-8")).decode("ascii")
    return rf'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$a = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json
$dir = Join-Path $env:TEMP 'orrery-wsl-anchor'
[void](New-Item -ItemType Directory -Force -Path $dir)
$list = @('-d', $a[0], '--exec') + @($a | Select-Object -Skip 1)
$p = Start-Process -FilePath "$env:SystemRoot\System32\wsl.exe" -ArgumentList $list `
  -WorkingDirectory $env:TEMP -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $dir 'stdout.txt') `
  -RedirectStandardError (Join-Path $dir 'stderr.txt')
[pscustomobject]@{{pid=$p.Id}} | ConvertTo-Json -Compress
'''


def start_windows_client(distro: str, argv: list[str], timeout: float = 30.0) -> tuple[int | None, str]:
    """Start the hidden wsl.exe. Returns (Windows PID, error)."""
    for arg in [distro, *argv]:
        if not _SAFE_ARG.match(arg):
            return None, f"cannot pass {arg!r} to wsl.exe (spaces or quotes); install under a plain path"
    shell = _powershell()
    if not shell:
        return None, "powershell.exe is not reachable (Windows interop off?)"
    encoded = base64.b64encode(start_script(distro, argv).encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            cwd="/", capture_output=True, timeout=timeout, env=interop_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"powershell.exe failed: {exc}"
    out = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        return None, f"powershell.exe exited {result.returncode}: {err[-1] if err else out}"
    for line in reversed(out.splitlines()):
        try:
            return int(json.loads(line)["pid"]), ""
        except (ValueError, KeyError, TypeError):
            continue
    return None, f"powershell.exe printed no PID: {out[:200]}"


# --------------------------------------------------------------------------- #
# The controller: a thread in the dashboard.
# --------------------------------------------------------------------------- #
class Controller:
    """Decides whether to hold, keeps one anchor while it should, reports why.

    `work` returns (reasons, pids, known): reasons is a dict of counts such
    as {"agents": 2, "launching": 1}; pids is a list of [pid, start_ticks]
    for the live agent processes; known is False when they could not be
    observed. Launch reservations and page leases are counted here.
    """

    def __init__(self, work, directory: pathlib.Path, distro: str, anchor_argv,
                 starter=start_windows_client, clock=time.time, sleep_ready=time.sleep):
        self.work = work
        self.dir = directory
        self.distro = distro
        self.anchor_argv = anchor_argv  # nonce -> argv run by wsl.exe --exec
        self.starter = starter
        self.clock = clock
        self.sleep_ready = sleep_ready
        self.last_busy: float | None = None
        self.failures = 0
        self.last_failure = 0.0
        self.error = ""
        self.windows_pid: int | None = None
        self.last_pids: list = []
        self.wake = threading.Event()

    def _ready(self, nonce: str) -> bool:
        deadline = self.clock() + READY_TIMEOUT_SECONDS
        while True:
            if _read_json(self.dir / "anchor.json").get("nonce") == nonce and anchor_running(self.dir):
                return True
            if self.clock() >= deadline:
                return False
            self.sleep_ready(0.5)

    def _start(self) -> None:
        now = self.clock()
        if self.failures >= MAX_START_FAILURES and now - self.last_failure < RETRY_AFTER_FAILURE_SECONDS:
            return
        nonce = uuid.uuid4().hex
        pid, error = self.starter(self.distro, self.anchor_argv(nonce))
        if pid is not None:
            # Keep it even if ready comes late: the client exists, and the
            # status must say which Windows process holds WSL.
            self.windows_pid = pid
            _write_json(self.dir / "client.json", {"windows_pid": pid, "nonce": nonce, "started": time.time()})
        if pid is not None and not error and self._ready(nonce):
            self.failures = 0
            self.error = ""
            return
        self.failures += 1
        self.last_failure = self.clock()
        self.error = error or "the anchor did not report ready"
        print(f"wsl-anchor: start failed ({self.failures}): {self.error}", file=sys.stderr, flush=True)

    def tick(self) -> dict:
        # Observing and deciding happen under work.lock, so a launch reserved
        # meanwhile is either counted here or reserved after this decision
        # (and then wakes the controller, which starts a new anchor).
        with _locked(self.dir):
            now = self.clock()
            reasons, pids, known = self.work()
            reasons = {key: int(value) for key, value in reasons.items() if value}
            real_now = time.time()  # file mtimes are wall-clock
            launching = fresh_reservations(self.dir, real_now)
            if launching:
                reasons["launching"] = reasons.get("launching", 0) + launching
            pages = fresh_leases(self.dir, real_now)
            if pages:
                reasons["pages"] = pages
            if known:
                # An agent seen before counts until its death is confirmed,
                # whatever tmux now says (a tmux that lost its server, a
                # process moved out of its pane).
                current = {int(entry[0]) for entry in pids}
                kept = [entry for entry in self.last_pids
                        if int(entry[0]) not in current and process_state(int(entry[0]), entry[1]) != "dead"]
                if kept:
                    pids = pids + kept
                    reasons["agents"] = len(pids)
                self.last_pids = pids
            else:
                # Cannot see the agents: keep the last confirmed ones and hold.
                reasons["unknown"] = 1
                pids = self.last_pids
            if reasons:
                self.last_busy = now
            grace = self.last_busy is not None and now - self.last_busy < GRACE_SECONDS
            holding = bool(reasons) or grace
            _write_json(self.dir / "wanted.json", {
                "hold": holding,
                "until": now + LEASE_SECONDS if holding else now,
                "pids": pids,
                "reasons": reasons,
                "updated": now,
            })
        running = anchor_running(self.dir)
        if holding and not running:
            self._start()
            running = anchor_running(self.dir)
        if not holding:
            self.failures = 0
            self.error = ""
        if not running:
            self.windows_pid = None
        elif self.windows_pid is None:
            # Started by an earlier dashboard, or ready came after a timeout:
            # the client that started the running anchor is on record.
            client = _read_json(self.dir / "client.json")
            if client.get("nonce") and client.get("nonce") == _read_json(self.dir / "anchor.json").get("nonce"):
                self.windows_pid = client.get("windows_pid")
        if holding and running:
            if "unknown" in reasons:
                state = "unknown"
            else:
                state = "holding" if reasons else "releasing"
        elif holding:
            state = "failed"
        else:
            state = "idle"
        status = {
            "state": state,
            "reasons": reasons,
            "agents": reasons.get("agents", 0),
            "anchor_running": running,
            "windows_pid": self.windows_pid,
            "anchor": _read_json(self.dir / "anchor.json") if running else {},
            "error": self.error if state == "failed" else "",
            "grace_seconds_left": (
                max(0, round(GRACE_SECONDS - (now - self.last_busy))) if state == "releasing" else 0
            ),
            "updated": now,
        }
        _write_json(self.dir / "status.json", status)
        return status

    def run_forever(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - never kill the dashboard
                print(f"wsl-anchor: tick failed: {exc}", file=sys.stderr, flush=True)
                try:
                    # Say so; the anchor keeps its last lease and known agents.
                    _write_json(self.dir / "status.json", {
                        "state": "error", "error": f"{type(exc).__name__}: {exc}",
                        "reasons": {}, "updated": time.time(),
                    })
                except OSError:
                    pass
            self.wake.wait(TICK_SECONDS)
            self.wake.clear()

    def settled(self, since: float, timeout: float) -> dict:
        return wait_settled(self.dir, since, timeout, self.wake.set)


SETTLED_STATES = ("holding", "unknown", "failed", "error")


def wait_settled(directory: pathlib.Path, since: float, timeout: float, nudge=None) -> dict:
    """The first status written at or after `since` that holds or has failed.

    An older status says nothing about this launch, so it is never returned:
    on timeout the answer is {"state": "timeout"}.
    """
    deadline = time.time() + timeout
    while True:
        status = _read_json(directory / "status.json")
        if float(status.get("updated") or 0) >= since and status.get("state") in SETTLED_STATES:
            return status
        if time.time() >= deadline:
            return {"state": "timeout", "error": "the WSL hold did not start in time"}
        if nudge:
            nudge()
        time.sleep(0.2)


def fresh_leases(directory: pathlib.Path, now: float | None = None) -> int:
    """Visible ORRERY pages: leases renewed within PAGE_LEASE_SECONDS."""
    return _fresh(directory / "leases", PAGE_LEASE_SECONDS, time.time() if now is None else now)


def fresh_reservations(directory: pathlib.Path, now: float | None = None) -> int:
    """Launches that have not settled, up to RESERVATION_SECONDS old."""
    return _fresh(directory / "reservations", RESERVATION_SECONDS, time.time() if now is None else now)


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "hold":
        return hold(pathlib.Path(argv[1]), argv[2])
    if len(argv) in (3, 5) and argv[0] == "reserve":
        # For shell launchers: reserve under work.lock, then wait for a
        # decision the controller made after it. Exit 0 when held, 1 when
        # the hold is not established (with the reason on stderr).
        directory, token = pathlib.Path(argv[1]), argv[2]
        wait = float(argv[4]) if len(argv) == 5 and argv[3] == "--wait" else 0.0
        since = time.time()
        try:
            reserve(directory, token)
        except (OSError, ValueError) as exc:
            print(f"WSL hold: cannot reserve this launch: {exc}", file=sys.stderr)
            return 1
        if wait <= 0:
            return 0
        status = _read_json(directory / "status.json")
        if not status or time.time() - float(status.get("updated") or 0) > 3 * TICK_SECONDS:
            print("WSL hold: the dashboard is not running, so nothing keeps WSL for this agent;"
                  " keep this window open", file=sys.stderr)
            return 1
        status = wait_settled(directory, since, wait)
        if status.get("state") in ("holding", "unknown"):
            return 0
        print(f"WSL hold: not established ({status.get('state')}: {status.get('error') or 'no reason given'});"
              " keep this window open", file=sys.stderr)
        return 1
    if len(argv) == 3 and argv[0] == "release":
        release_reservation(pathlib.Path(argv[1]), argv[2])
        return 0
    if len(argv) == 1 and argv[0] == "status":
        print(json.dumps(_read_json(state_dir() / "status.json"), sort_keys=True))
        return 0
    print("usage: wsl_anchor.py hold STATE_DIR NONCE | reserve STATE_DIR TOKEN [--wait S]"
          " | release STATE_DIR TOKEN | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
