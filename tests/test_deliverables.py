"""Coverage for the generic dashboard deliverables index."""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import dashboard.server as server


def _write_log(directory, filename: str, agent: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(
        f"---\nagent: {agent}\ntags: [claude]\n---\n\n## Goal\nTest output.\n",
        encoding="utf-8",
    )


def _reset_cache(monkeypatch) -> None:
    monkeypatch.setattr(
        server, "_DELIV_CACHE", {"ts": 0.0, "key": None, "map": None}
    )


def test_api_finds_project_logs_without_vault(monkeypatch, tmp_path):
    project = tmp_path / "project"
    log_name = "LOG_2026-08-01T0900 Generic Project.md"
    _write_log(project / "logs", log_name, "WiseFaraday")
    _write_log(project / "private-layout", "LOG_2026-08-01T0800 Hidden.md", "WiseFaraday")

    monkeypatch.setattr(server, "PROJECT_KEY", str(project))
    monkeypatch.setattr(server, "VAULT", "")
    monkeypatch.delenv("AGENTSTACK_DELIVERABLE_ROOTS", raising=False)
    _reset_cache(monkeypatch)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        query = urllib.parse.urlencode({"agent": "WiseFaraday"})
        with urllib.request.urlopen(
            f"http://127.0.0.1:{httpd.server_port}/api/deliverables?{query}"
        ) as response:
            payload = json.load(response)
    finally:
        httpd.shutdown()
        thread.join()

    assert payload["ok"] is True
    assert payload["vault"] == ""
    assert payload["items"] == [
        {
            "title": log_name[:-3],
            "rel": log_name,
            "vault": "",
            "mtime": int((project / "logs" / log_name).stat().st_mtime),
        }
    ]


def test_configured_roots_override_default_and_keep_vault_links(monkeypatch, tmp_path):
    project = tmp_path / "project"
    vault = tmp_path / "vault"
    shared = tmp_path / "shared logs"
    _write_log(project / "logs", "LOG_2026-08-01T0700 Default.md", "WiseFaraday")
    _write_log(vault / "team-logs", "LOG_2026-08-01T0800 Vault.md", "WiseFaraday")
    _write_log(shared, "LOG_2026-08-01T0900 Shared.md", "WiseFaraday")
    os.utime(vault / "team-logs" / "LOG_2026-08-01T0800 Vault.md", (100, 100))
    os.utime(shared / "LOG_2026-08-01T0900 Shared.md", (200, 200))

    monkeypatch.setattr(server, "PROJECT_KEY", str(project))
    monkeypatch.setattr(server, "VAULT", str(vault))
    monkeypatch.setenv(
        "AGENTSTACK_DELIVERABLE_ROOTS",
        os.pathsep.join((str(vault / "team-logs"), str(shared))),
    )
    _reset_cache(monkeypatch)

    items = server._deliverables_index()["WiseFaraday"]

    assert [item["title"] for item in items] == [
        "LOG_2026-08-01T0900 Shared",
        "LOG_2026-08-01T0800 Vault",
    ]
    by_title = {item["title"]: item for item in items}
    assert by_title["LOG_2026-08-01T0800 Vault"]["vault"] == "vault"
    assert by_title["LOG_2026-08-01T0800 Vault"]["rel"] == os.path.join(
        "team-logs", "LOG_2026-08-01T0800 Vault.md"
    )
    assert by_title["LOG_2026-08-01T0900 Shared"]["vault"] == ""
    assert by_title["LOG_2026-08-01T0900 Shared"]["rel"] == (
        "LOG_2026-08-01T0900 Shared.md"
    )
