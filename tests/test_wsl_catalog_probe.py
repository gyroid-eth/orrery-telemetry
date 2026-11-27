import logging
import os
import sys
import threading
from types import SimpleNamespace

import pytest

from dashboard import wsl_catalog_probe as probe


def test_catalog_requests_share_one_worker_and_refresh_after_completion(monkeypatch):
    clock = SimpleNamespace(now=10)
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=lambda: clock.now))
    catalog = probe.CatalogProbe()
    entered = threading.Event()
    release = threading.Event()
    workers = []
    outcome = probe.WindowsCatalog("/mnt/c/Users/example", "ready", "", retry=False)

    def check():
        workers.append(threading.current_thread())
        entered.set()
        if not release.wait(5):
            raise TimeoutError
        return outcome

    try:
        assert catalog.read(check).retry
        assert entered.wait(5)
        clock.now += 20
        assert catalog.read(check).retry
        assert catalog.read(check).retry
        assert len(workers) == 1
    finally:
        release.set()
        if workers:
            workers[0].join(5)

    assert catalog.read(check) == outcome
    assert len(workers) == 1
    clock.now += probe.SUCCESS_TTL
    entered.clear()
    assert catalog.read(check) == outcome
    assert entered.wait(5)
    workers[1].join(5)


@pytest.mark.skipif(sys.platform == "win32", reason="WSL pipe selection uses POSIX descriptors")
@pytest.mark.parametrize("finishes", [True, False])
def test_powershell_phase_log_includes_partial_timings_and_hides_profile(caplog, finishes):
    command = r"""
import time
print("ORRERY_PROBE|started|0|3", flush=True)
print("ORRERY_PROBE|profile|4|C:\\Users\\example", flush=True)
"""
    command += (
        'print("ORRERY_PROBE|finished|7|", flush=True)' if finishes else "time.sleep(30)"
    )
    with caplog.at_level(logging.INFO, logger=probe.LOG.name):
        status, code, phases = probe.run_phased_probe(
            "powershell", [sys.executable, "-c", command], env=os.environ.copy(), timeout=0.8)
    assert status == ("ok" if finishes else "powershell_timeout")
    assert phases["profile"].value == r"C:\Users\example"
    assert phases["started"].arrival_ms <= phases["profile"].arrival_ms
    assert ("finished" in phases) is finishes
    assert code == (0 if finishes else None)
    assert "entry_ms=" in caplog.text
    assert "powershell_start_ms=3" in caplog.text
    assert "'profile': 4" in caplog.text
    assert "example" not in caplog.text
