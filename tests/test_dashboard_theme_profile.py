from __future__ import annotations

import base64
import contextlib
import glob
import json
import os
import random
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
FIXTURE = Path(__file__).parent / "fixtures" / "dashboard_theme_profile_order.js"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


class _WebSocket:
    def __init__(self, url: str) -> None:
        _, rest = url.split("://", 1)
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.socket = socket.create_connection((host, int(port)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.socket.sendall(
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.socket.recv(1)
        self.buffer = response.split(b"\r\n\r\n", 1)[1]
        self.request_id = 0

    def _receive(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.socket.recv(65536)
            if not chunk:
                raise EOFError("CDP websocket closed")
            self.buffer += chunk
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result

    def _message(self) -> dict[str, object]:
        _first, second = self._receive(2)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack("!H", self._receive(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self._receive(8))[0]
        return json.loads(self._receive(size))

    def call(self, method: str, **params: object) -> dict[str, object]:
        self.request_id += 1
        request_id = self.request_id
        data = json.dumps({"id": request_id, "method": method, "params": params}).encode()
        size = len(data)
        header = b"\x81"
        if size < 126:
            header += struct.pack("!B", size | 0x80)
        elif size < 65536:
            header += struct.pack("!BH", 126 | 0x80, size)
        else:
            header += struct.pack("!BQ", 127 | 0x80, size)
        mask = struct.pack("!I", random.getrandbits(32))
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        self.socket.sendall(header + mask + payload)
        while True:
            message = self._message()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(message["error"])
            return message.get("result", {})


def _find_chrome() -> str | None:
    configured = os.environ.get("AGENTSTACK_CHROME")
    if configured:
        return configured
    candidates = sorted(
        glob.glob(
            str(
                Path.home()
                / "Library/Caches/ms-playwright/chromium_headless_shell-*"
                / "chrome-headless-shell-*"
                / "chrome-headless-shell"
            )
        ),
        reverse=True,
    )
    candidates.extend(
        [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            shutil.which("google-chrome"),
        ]
    )
    return next(
        (str(candidate) for candidate in candidates if candidate and Path(candidate).is_file()),
        None,
    )


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


def _run_browser_contract(chrome: str | None = None) -> dict[str, object]:
    executable = chrome or _find_chrome()
    if not executable:
        raise RuntimeError("Chromium not found; set AGENTSTACK_CHROME")
    handler = partial(_QuietHandler, directory=str(DASHBOARD))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    cdp_port = _free_port()
    profile = tempfile.mkdtemp(prefix="agentstack-theme-cdp-")
    process = subprocess.Popen(
        [
            executable,
            "--headless=new",
            "--no-sandbox",
            "--single-process",
            "--disable-gpu",
            f"--remote-debugging-port={cdp_port}",
            "--window-size=1600,1000",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        tabs = None
        for _ in range(80):
            try:
                tabs = json.load(
                    urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json")
                )
                break
            except OSError:
                time.sleep(0.1)
        if not tabs:
            raise RuntimeError("Chromium CDP endpoint did not start")
        page = next(tab for tab in tabs if tab["type"] == "page")
        client = _WebSocket(page["webSocketDebuggerUrl"])
        client.call("Page.enable")
        client.call("Runtime.enable")
        client.call("Network.enable")
        client.call("Network.setCacheDisabled", cacheDisabled=True)
        client.call(
            "Page.navigate",
            url=f"http://127.0.0.1:{server.server_port}/index.html?embed=1&demo=1",
        )
        for _ in range(80):
            ready = (
                client.call(
                    "Runtime.evaluate",
                    expression="document.readyState",
                    returnByValue=True,
                )
                .get("result", {})
                .get("value")
            )
            if ready == "complete":
                break
            time.sleep(0.1)
        time.sleep(0.5)
        evaluated = client.call(
            "Runtime.evaluate",
            expression=FIXTURE.read_text(),
            awaitPromise=True,
            returnByValue=True,
        )
        if evaluated.get("exceptionDetails"):
            raise AssertionError(evaluated["exceptionDetails"])
        return evaluated["result"]["value"]
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            process.kill()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        shutil.rmtree(profile, ignore_errors=True)


def _assert_browser_contract(result: dict[str, object]) -> None:
    normal = result["normal"]
    assert len(normal) == 20
    for record in normal:
        assert record["ok"] is True, record
        effect = record["effect"]
        assert effect["visibleReached"] == effect["visibleExpected"] > 0, record
        assert effect["visibleChanged"] > 0 and effect["changed"] > 0, record
    for key in ("zero", "minimum"):
        records = result[key]
        assert len(records) == 5
        for record in records:
            assert record["ok"] is False, record
            assert record["reason"] == "no-effective-change", record

    endpoint = result["adversarial"]["alreadyAtEndpoint"]
    assert endpoint["beforeMembership"] == endpoint["afterMembership"] > 0
    assert endpoint["applied"]["ok"] is True
    effect = endpoint["applied"]["effect"]
    assert effect["visibleReached"] == effect["visibleExpected"] > 0
    assert effect["visibleChanged"] < effect["visibleExpected"]

    blocked = result["adversarial"]["importantAndPreapplyMismatch"]
    assert blocked["lastValid"]["ok"] is True
    assert blocked["beforeMembership"] == blocked["afterMembership"] > 0
    assert blocked["rejected"]["ok"] is False
    assert blocked["rejected"]["reason"] == "effect-count-mismatch"
    effect = blocked["rejected"]["effect"]
    assert effect["visibleReached"] < effect["visibleExpected"]
    assert blocked["rolledBackState"]["axis"] == "dim-contrast"
    assert blocked["rolledBackState"]["value"] == 0.25

    hidden = result["adversarial"]["hidden"]
    assert hidden["ok"] is False and hidden["reason"] == "no-visible-targets"
    assert result["final"]["axis"] is None


def test_dashboard_browser_fixture_uses_full_guard_paths():
    source = FIXTURE.read_text()
    assert "applyThemeAxisMessage" in source
    assert "Number.MIN_VALUE" in source
    assert "!important" in source
    assert "beforeMembership" in source and "afterMembership" in source
    assert "alreadyAtEndpoint" in source
    assert "rolledBackState" in source


@pytest.mark.skipif(
    os.environ.get("AGENTSTACK_THEME_BROWSER") != "1",
    reason="set AGENTSTACK_THEME_BROWSER=1 to run the real Chromium guard contract",
)
def test_dashboard_theme_guard_in_real_browser():
    _assert_browser_contract(_run_browser_contract())
