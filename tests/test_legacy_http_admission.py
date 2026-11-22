"""Exercise authority admission on the real legacy HTTP handler, offline."""
from __future__ import annotations

import http.client
import json
import socket
import threading
from email.message import Message

import pytest

from dashboard import server as dashboard
from dashboard import legacy_http_admission as admission
from _dashboard_http_fixture import dashboard_http_fixture


@pytest.fixture
def http_dashboard(monkeypatch):
    monkeypatch.setattr(dashboard, "GLOBAL_MODE", False)
    monkeypatch.delenv("AGENTSTACK_DASHBOARD_ALLOWED_ORIGINS", raising=False)
    calls = []

    class Answer:
        def __init__(self, *_args):
            calls.append("roster")

        def body(self, _scope):
            return b'{"agents":[],"fixture":true}'

    class Handler(dashboard.Handler):
        protocol_version = "HTTP/1.1"

        def _single_flight(self, _key, compute):
            return compute()

    monkeypatch.setattr(dashboard, "_AgentsAnswer", Answer)
    monkeypatch.setattr(dashboard, "do_exit", lambda session: calls.append(session) or {"ok": True})
    httpd = dashboard_http_fixture(dashboard, handler=Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_port, calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def request(port, method="GET", host=None, origin=None, path=None, extra=()):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    body = json.dumps({"session": "FixtureAgent"}).encode() if method == "POST" else b""
    headers = [("Host", host or f"127.0.0.1:{port}")]
    if origin is not None:
        headers.append(("Origin", origin))
    headers.extend(extra)
    if method == "POST":
        headers.extend((("Content-Type", "application/json"), ("Content-Length", str(len(body)))))
    try:
        connection.putrequest(method, path or ("/api/exit" if method == "POST" else "/api/agents"), skip_host=True)
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_untrusted_host_is_rejected_before_dispatch(http_dashboard, method):
    port, calls = http_dashboard
    status, _ = request(port, method, host=f"attacker.example:{port}",
                        origin=f"http://attacker.example:{port}")
    assert (status, calls) == (403, [])


def test_loopback_cli_and_same_origin_keep_the_real_listener_port(http_dashboard):
    port, calls = http_dashboard
    for host in (f"127.0.0.1:{port}", f"LOCALHOST:{port}", f"[::1]:{port}"):
        assert request(port, "POST", host=host)[0] == 200
        assert request(port, "POST", host=host, origin="http://" + host)[0] == 200
    assert calls == ["FixtureAgent"] * 6
    assert json.loads(request(port)[1]) == {"agents": [], "fixture": True}


@pytest.mark.parametrize("method", ["GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_every_verb_and_route_requires_trusted_host(http_dashboard, method):
    port, calls = http_dashboard
    for path in ("/", "/api/agents", "/future-operation"):
        assert request(port, method, host="attacker.example", path=path)[0] == 403
    assert not calls


def test_untrusted_authorities_and_forwarded_headers_are_not_inferred(http_dashboard, monkeypatch):
    port, calls = http_dashboard
    monkeypatch.setattr(dashboard, "BIND_HOST", "0.0.0.0")
    for host in ("localhost", f"localhost:{port + 1}", f"localhost.attacker.example:{port}",
                 f"127.1:{port}", "0.0.0.0:" + str(port), "::1:" + str(port),
                 "[::1]", f"user@localhost:{port}", f"localhost:{port},attacker.example",
                 f"localhost:{port}/", "localhost:0", "localhost:65536", "localhost:"):
        assert request(port, host=host, extra=[("X-Forwarded-Host", f"localhost:{port}"),
                                             ("Forwarded", f"host=localhost:{port}")])[0] == 403
    assert not calls


def test_fixed_origins_match_the_selected_host_only(http_dashboard, monkeypatch):
    port, calls = http_dashboard
    monkeypatch.setenv(admission.SETTING, "http://dashboard.example:8770 https://secure.example http://plain.example")
    for host, origin in (("DASHBOARD.EXAMPLE:8770", "http://dashboard.example:8770"),
                         ("secure.example", "https://SECURE.EXAMPLE:443"),
                         ("secure.example:443", "https://secure.example"),
                         ("plain.example", "http://plain.example:80")):
        assert request(port, "POST", host=host, origin=origin)[0] == 200
    assert request(port, host="secure.example", origin="https://secure.example", path="/api/quotas")[0] == 200
    calls.clear()
    for host, origin in (("dashboard.example:8770", "https://secure.example"),
                         ("dashboard.example", "http://dashboard.example:8770"),
                         ("secure.example:80", "https://secure.example"),
                         ("secure.example", "http://secure.example"),
                         (f"localhost:{port}", "http://plain.example"),
                         (f"localhost:{port}", "null")):
        assert request(port, "POST", host=host, origin=origin)[0] == 403
    assert not calls


def test_absolute_request_targets_cannot_switch_authority(http_dashboard, monkeypatch):
    port, calls = http_dashboard
    monkeypatch.setenv(admission.SETTING, "https://dashboard.example https://other.example")
    assert request(port, host="dashboard.example", path="https://dashboard.example:443/api/agents")[0] == 200
    calls.clear()
    for path in ("https://other.example/api/agents", "http://dashboard.example/api/agents",
                 "https://dashboard.example:444/api/agents", "https://user@dashboard.example/api/agents"):
        assert request(port, host="dashboard.example", path=path)[0] == 403
    assert not calls


def test_existing_post_and_sensitive_get_guards_remain(http_dashboard):
    port, calls = http_dashboard
    assert request(port, "POST", origin="http://attacker.example")[0] == 403
    for value in ("cross-site", "same-site", "none"):
        assert request(port, "POST", extra=[("Sec-Fetch-Site", value)])[0] == 403
    assert request(port, origin="http://attacker.example", path="/api/quotas")[0] == 403
    assert request(port, path="/api/quotas")[0] == 200
    assert not calls


def test_json_and_media_type_requirements_remain(http_dashboard):
    port, calls = http_dashboard
    for body, content_type, status in ((b"{}", "text/plain", 415), (b"broken", "application/json", 400),
                                       (b"[]", "application/json", 400)):
        headers = (f"POST /api/exit HTTP/1.1\r\nHost: localhost:{port}\r\nConnection: close\r\n"
                   f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n\r\n").encode()
        assert raw(port, headers + body).startswith(f"HTTP/1.1 {status} ".encode())
    assert not calls


def raw(port, payload):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as stream:
        stream.sendall(payload)
        parts = []
        while chunk := stream.recv(8192):
            parts.append(chunk)
    return b"".join(parts)


def test_ambiguous_headers_close_before_body_or_pipelined_dispatch(http_dashboard):
    port, calls = http_dashboard
    valid = f"Host: localhost:{port}\r\n"
    for headers in ("", "Host:\r\n", valid + valid, valid + "Origin: http://one.example\r\nOrigin: http://two.example\r\n",
                    valid + "Authority: attacker.example\r\n", "Host: localhost:\r\n",
                    f"Host: localhost:{port}\r\n folded\r\n"):
        payload = ("POST /api/exit HTTP/1.1\r\n" + headers +
                   "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}" +
                   "GET /api/agents HTTP/1.1\r\n" + valid + "Connection: close\r\n\r\n").encode()
        response = raw(port, payload)
        assert response.startswith(b"HTTP/1.1 403"), response
        assert response.count(b"HTTP/1.1 ") == 1
        assert b"Connection: close\r\n" in response
    assert not calls


def test_keepalive_revalidates_each_request(http_dashboard):
    port, calls = http_dashboard
    with socket.create_connection(("127.0.0.1", port), timeout=3) as stream:
        stream.sendall(f"GET /api/agents HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n".encode())
        response = http.client.HTTPResponse(stream)
        response.begin()
        assert response.status == 200
        response.read()
        stream.sendall(b"GET /api/agents HTTP/1.1\r\nHost: attacker.example\r\n\r\n")
        response = http.client.HTTPResponse(stream)
        response.begin()
        assert response.status == 403
        response.read()
    assert calls == ["roster"]


@pytest.mark.parametrize("value", ["https://*.example", "https://example/path", "https://user@example",
                                    "http://example?x", "http://example#x", "null", "https://example:0",
                                    "https://example:65536", "http://example\nhttps://other.example",
                                    "http://example\thttps://other.example", 'http://example"',
                                    "http://example';print(1)", "http://$(id)", "http://`id`",
                                    "https://[::1%lo0]", "https://[not-ip]"])
def test_invalid_settings_fail_closed_on_real_handler(http_dashboard, monkeypatch, value):
    port, calls = http_dashboard
    with pytest.raises(ValueError, match=admission.SETTING):
        admission.configured_origins(value)
    monkeypatch.setenv(admission.SETTING, value)
    assert request(port)[0] == 403
    assert not calls


def test_normalized_authorities_and_default_ports():
    extra = " HTTP://Dashboard.Example:80   https://[0:0:0:0:0:0:0:1]:443 "
    assert admission.configured_origins(extra) == frozenset({("http", "dashboard.example", 80), ("https", "::1", 443)})
    for host, expected in (("DASHBOARD.EXAMPLE", ("http", "dashboard.example", 80)),
                           ("dashboard.example:80", ("http", "dashboard.example", 80)),
                           ("[::1]", ("https", "::1", 443))):
        headers = Message()
        headers["Host"] = host
        assert expected in admission.admit(headers, "/api/agents", 18770, extra)


def test_invalid_setting_stops_main_before_legacy_workers(monkeypatch):
    monkeypatch.setattr(dashboard, "GLOBAL_MODE", False)
    monkeypatch.setenv(admission.SETTING, "http://*.example")
    monkeypatch.setattr(dashboard, "_start_supervisor_watchdog", lambda: pytest.fail("legacy worker started"))
    with pytest.raises(ValueError, match=admission.SETTING):
        dashboard.main()
