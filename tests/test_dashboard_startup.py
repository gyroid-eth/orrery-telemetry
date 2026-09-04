"""Dashboard startup must not require the optional stale-ttyd cleanup tool."""

from __future__ import annotations

import subprocess
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import dashboard.server as server


@pytest.mark.parametrize("cleanup_status", ["missing", 0, 1])
def test_main_serves_dashboard_with_optional_pkill(monkeypatch, cleanup_status):
    """Exercise main's real HTTP bind and handler without a persistent server."""
    cleanup = Mock()
    if cleanup_status == "missing":
        cleanup.side_effect = FileNotFoundError("pkill is not installed")
    else:
        cleanup.return_value = subprocess.CompletedProcess([], cleanup_status)
    monkeypatch.setattr(server, "subprocess", SimpleNamespace(run=cleanup))
    monkeypatch.setattr(server, "_start_supervisor_watchdog", lambda: None)
    monkeypatch.setattr(server, "_ttyd_reaper", lambda: None)
    monkeypatch.setattr(server, "BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "PORT", 0)

    class SingleRequestServer(ThreadingHTTPServer):
        def serve_forever(self):
            self.timeout = 5
            worker = threading.Thread(target=self.handle_request, daemon=True)
            worker.start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.server_port}/", timeout=5
                ) as response:
                    assert response.status == 200
                    assert b"AGENTSTACK_SERVER_DEFAULTS" in response.read()
            finally:
                worker.join(timeout=6)
                self.server_close()
            assert not worker.is_alive()

    monkeypatch.setattr(server, "ThreadingHTTPServer", SingleRequestServer)

    server.main()

    cleanup.assert_called_once_with(
        ["pkill", "-f", "ttyd -p .* tmux attach -t ="],
        capture_output=True,
    )
