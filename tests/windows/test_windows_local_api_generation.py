"""The Windows launcher accepts any orrery-telemetry API generation (2026-09-30).

`api` in /api/version went from 1 to 2; dashboard_health only checks that the
port serves ORRERY Telemetry, so it must not insist on one generation. This
runs on any OS: it calls dashboard_health with a stubbed request.
"""
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("psutil")
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('windows_local', ROOT / 'scripts/windows/windows_local.py')
local = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local)


@pytest.mark.parametrize("api, accepted", [(1, True), (2, True), (3, True), (0, False), ("2", False), (True, False), (None, False)])
def test_dashboard_health_accepts_any_api_generation(monkeypatch, api, accepted):
    def request(url, payload=None):
        if url.endswith('api/version'):
            answer = {'name': 'orrery-telemetry', 'version': 'test'}
            if api is not None:
                answer['api'] = api
            return answer
        return {}

    monkeypatch.setattr(local, 'request', request)
    if accepted:
        local.dashboard_health('http://127.0.0.1:8770/')
    else:
        with pytest.raises(RuntimeError, match='not the expected ORRERY API'):
            local.dashboard_health('http://127.0.0.1:8770/')
