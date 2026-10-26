"""Provenance properties on a private dashboard HTTP listener."""
import pytest

from test_legacy_http_admission import http_dashboard, request
from dashboard import server as dashboard


@pytest.mark.parametrize('method', ['GET', 'POST', 'HEAD', 'OPTIONS', 'PUT'])
@pytest.mark.parametrize('path', ['/', '/api/version', '/api/agents', '/future-operation'])
def test_external_provenance_is_denied_before_dispatch(http_dashboard, method, path):
    port, calls = http_dashboard
    for origin in ('https://external.example', 'null', '', f'http://127.0.0.1:{port + 1}'):
        assert request(port, method, origin=origin, path=path)[0] == 403
    for mode in ('cors', 'no-cors', 'websocket', ''):
        assert request(port, method, path=path, extra=[
            ('Sec-Fetch-Site', 'cross-site'), ('Sec-Fetch-Mode', mode)])[0] == 403
    assert not calls


def test_own_page_local_clients_and_navigation_are_admitted(http_dashboard):
    port, _ = http_dashboard
    for headers in ([], [('Origin', f'http://127.0.0.1:{port}')],
                    [('Sec-Fetch-Site', 'cross-site'), ('Sec-Fetch-Mode', 'navigate')]):
        assert request(port, path='/api/version', extra=headers)[0] == 200


def test_global_static_shell_has_the_same_network_guard(http_dashboard, monkeypatch):
    port, _ = http_dashboard
    monkeypatch.setattr(dashboard, 'GLOBAL_MODE', True)
    for path in ('/', '/theme_light.css'):
        assert request(port, path=path, host=f'external.example:{port}')[0] == 403
        assert request(port, path=path, origin='null')[0] == 403
    assert request(port, path='/')[0] == 200


def test_ambiguous_provenance_is_denied(http_dashboard):
    port, _ = http_dashboard
    for headers in ([('Sec-Fetch-Site', 'same-origin'), ('Sec-Fetch-Site', 'cross-site')],
                    [('Sec-Fetch-Mode', 'navigate'), ('Sec-Fetch-Mode', 'no-cors')]):
        assert request(port, path='/api/version', extra=headers)[0] == 403


def test_retired_terminal_route_is_absent(http_dashboard, monkeypatch):
    port, _ = http_dashboard
    monkeypatch.setattr(dashboard.subprocess, 'Popen', lambda *args, **kwargs:
                        pytest.fail('retired route started a process'))
    assert request(port, path='/api/ptty?session=FixtureAgent')[0] == 404
