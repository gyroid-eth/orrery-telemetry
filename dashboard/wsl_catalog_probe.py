"""Share one demand-driven Windows capability check across catalog requests."""

from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
import selectors
import subprocess
import threading
import time


LOG = logging.getLogger(__name__)
PROBE_PREFIX = b"ORRERY_PROBE|"
SUCCESS_TTL = 15
RETRY_TTL = 5


def enable_timing_log():
    """Send WSL measurements to the dashboard service log."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False


@dataclass(frozen=True)
class WindowsCatalog:
    profile: str | None = None
    chrome_state: str = "interop"
    computer_reason: str | None = None
    retry: bool = True


class CatalogProbe:
    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._expires = 0.0
        self._snapshot = WindowsCatalog()

    def read(self, check: Callable[[], WindowsCatalog]) -> WindowsCatalog:
        """Return the latest completed check while at most one worker refreshes it."""
        with self._lock:
            if not self._running and time.monotonic() >= self._expires:
                self._running = True
                worker = threading.Thread(target=self._refresh, args=(check,),
                                          name="wsl-catalog-probe", daemon=True)
                try:
                    worker.start()
                except RuntimeError:
                    self._running = False
                    self._expires = time.monotonic() + RETRY_TTL
                    LOG.warning("WSL catalog probe worker could not start")
            return self._snapshot

    def _refresh(self, check: Callable[[], WindowsCatalog]) -> None:
        try:
            snapshot = check()
        except Exception as exc:
            LOG.warning("WSL catalog probe failed (%s)", type(exc).__name__)
            snapshot = WindowsCatalog()
        with self._lock:
            self._snapshot = snapshot
            self._expires = time.monotonic() + (RETRY_TTL if snapshot.retry else SUCCESS_TTL)
            self._running = False


@dataclass(frozen=True)
class WindowsPhase:
    script_ms: int
    arrival_ms: int
    value: str


def _read_windows_phases(process, started, deadline, phases):
    buffer = b""
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.select(max(0, deadline - time.monotonic())):
            block = os.read(process.stdout.fileno(), 4096)
            if not block:
                return
            arrival_ms = round((time.monotonic() - started) * 1000)
            lines = (buffer + block).split(b"\n")
            buffer = lines.pop()
            for line in lines:
                name, separator, value = line.removeprefix(PROBE_PREFIX).rstrip(b"\r").partition(b"|")
                if not line.startswith(PROBE_PREFIX) or not separator:
                    continue
                elapsed, separator, data = value.partition(b"|")
                if name.isalpha() and separator and 0 < len(elapsed) <= 8 and elapsed.isdigit():
                    try:
                        value = data.decode("utf-8")
                    except UnicodeError:
                        continue
                    phases[name.decode("ascii")] = WindowsPhase(int(elapsed), arrival_ms, value)
            if time.monotonic() >= deadline:
                break
    raise subprocess.TimeoutExpired(process.args, deadline - started)


def run_phased_probe(name: str, argv: list[str], *, env: dict[str, str], timeout: float):
    """Collect timing markers even when Windows times out before exiting."""
    started = time.monotonic()
    deadline = started + timeout
    phases = {}
    spawn_ms = None
    status, code = f"{name}_start_failed", None
    try:
        with subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            spawn_ms = round((time.monotonic() - started) * 1000)
            try:
                _read_windows_phases(process, started, deadline, phases)
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
                status = f"{name}_nonzero_exit={code}" if code else "ok"
            except subprocess.TimeoutExpired:
                status = f"{name}_timeout"
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    except (OSError, ValueError):
        pass
    entry = phases.get("started")
    end = phases.get("finished")
    total_ms = round((time.monotonic() - started) * 1000)
    powershell_start_ms = None
    dispatch_and_pipe_ms = None
    if name == "powershell" and entry and spawn_ms is not None and 0 < len(entry.value) <= 8 and entry.value.isdecimal():
        powershell_start_ms = int(entry.value)
        # Includes Linux/Windows dispatch and transit of the first stdout marker.
        dispatch_and_pipe_ms = max(0, entry.arrival_ms - spawn_ms - powershell_start_ms - entry.script_ms)
    LOG.log(logging.INFO if status == "ok" else logging.WARNING,
            "WSL %s probe status=%s spawn_ms=%s entry_ms=%s "
            "powershell_start_ms=%s dispatch_and_pipe_ms=%s script_ms=%s "
            "exit_ms=%s total_ms=%d phases_ms=%s",
            name, status, spawn_ms, entry.arrival_ms if entry else None,
            powershell_start_ms, dispatch_and_pipe_ms,
            end.script_ms if end else None, total_ms - end.arrival_ms if end else None,
            total_ms, {name: phase.script_ms for name, phase in phases.items()})
    return status, code, phases
