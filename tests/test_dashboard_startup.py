"""Dashboard startup serves without an external cleanup process."""

from __future__ import annotations

import threading
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import dashboard.server as server

pytestmark = pytest.mark.usefixtures("isolated_provider_stall_runtime")


def test_main_serves_dashboard_without_cleanup_process(monkeypatch):
    """Exercise main's real HTTP bind and handler without a persistent server."""
    cleanup = Mock()
    monkeypatch.setattr(server, "subprocess", SimpleNamespace(run=cleanup))
    monkeypatch.setattr(server, "_start_supervisor_watchdog", lambda: None)
    # Its first pass reads the real runtime and runs tmux per child; a thread
    # left running from here called later tests' subprocess stubs.
    monkeypatch.setattr(server, "_start_child_resume_maintenance", lambda: None)
    monkeypatch.setattr(server, "_start_provider_stall_monitor", lambda: None)
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

    cleanup.assert_not_called()
