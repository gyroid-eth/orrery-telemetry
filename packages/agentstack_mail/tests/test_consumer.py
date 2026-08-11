from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from agentstack_mail import consumer
from agentstack_mail.consumer import (
    ConsumerError,
    apply,
    main,
    prepare,
    preview,
    rollback,
    status,
)
from agentstack_mail.migration import MANIFEST_NAME as MIGRATION_MANIFEST_NAME
from agentstack_mail.migration import StatePaths, copy_state


_REAL_ASSERT_DATA_REVERSIBLE = consumer._assert_data_reversible
_REAL_WITH_AUTHORITY_LOCK = consumer._with_authority_lock


@pytest.fixture(autouse=True)
def _reversible_migration_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(consumer, "_assert_data_reversible", lambda *_: None)
    monkeypatch.setattr(consumer, "_with_authority_lock", lambda _: nullcontext())


def _rollback(
    bundle: Path,
    digest: str,
    *,
    fault_hook: object | None = None,
) -> dict[str, object]:
    return rollback(
        bundle,
        digest,
        bundle / "migration-manifest.json",
        "C5_CLIENT_SWITCHING",
        fault_hook=fault_hook,  # type: ignore[arg-type]
    )


def _json(path: Path, value: object, *, newline: bool) -> None:
    payload = json.dumps(value, indent=2, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + ("\n" if newline else ""), encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[Path, tuple[bytes, int]]]:
    home = tmp_path / "copied-home"
    old_db = str(home / "mcp_agent_mail" / "storage.sqlite3")
    new_db = str(home / ".agentstack" / "mail" / "storage.sqlite3")
    old_env = str(home / "mcp_agent_mail" / ".env")
    new_env = str(home / ".agentstack" / "agentstack-mail.env")
    old_home = str(home / ".mcp_agent_mail")
    new_home = str(home / ".agentstack" / "mail")
    old_signals = str(home / ".mcp_agent_mail" / "signals")
    new_signals = str(home / ".agentstack" / "mail" / "signals")
    old_url = "http://127.0.0.1:8765/mcp"
    new_url = "http://127.0.0.1:18765/mcp"

    claude = home / ".claude.json"
    _json(
        claude,
        {
            "theme": "dark",
            "mcpServers": {
                "mcp-agent-mail": {
                    "type": "http",
                    "url": "http://127.0.0.1:8765/api/",
                    "headers": {"Authorization": "Bearer fixture-secret"},
                },
                "notion": {"type": "http", "url": "https://example.test/mcp"},
            },
        },
        newline=False,
    )
    settings = home / ".claude" / "settings.json"
    _json(
        settings,
        {
            "permissions": {
                "allow": [
                    "Bash(git status:*)",
                    "mcp__mcp-agent-mail__register_agent",
                    "mcp__mcp-agent-mail__send_message",
                    "mcp__mcp-agent-mail__search_messages",
                    "mcp__mcp-agent-mail__summarize_thread",
                ]
            },
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "mcp__mcp-agent-mail__register_agent",
                        "hooks": [{"type": "command", "command": "check"}],
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": "mcp__mcp-agent-mail__register_agent",
                        "hooks": [{"type": "command", "command": "verify"}],
                    }
                ],
            },
        },
        newline=True,
    )
    local_settings = home / "vault" / ".claude" / "settings.local.json"
    _json(
        local_settings,
        {
            "permissions": {
                "allow": [
                    "mcp__mcp-agent-mail__fetch_inbox",
                    "mcp__mcp-agent-mail__summarize_recent",
                ]
            }
        },
        newline=True,
    )
    codex = home / ".codex" / "config.toml"
    codex.parent.mkdir(parents=True)
    codex.write_text(
        'model = "gpt-5.6-sol"\n\n'
        '# preserve this comment\n'
        '[mcp_servers.agent-mail]\n'
        'url = "http://127.0.0.1:8765/api/"\n'
        'bearer_token_env_var = "MCP_AGENT_MAIL_TOKEN"\n\n'
        '[mcp_servers.agent-mail.tools.fetch_inbox]\n'
        'approval_mode = "never"\n\n'
        '[mcp_servers.agent-mail.tools.search_messages]\n'
        'approval_mode = "never"\n\n'
        '[mcp_servers.agent-mail.tools.summarize_thread]\n'
        'approval_mode = "never"\n\n'
        '[mcp_servers.notion]\n'
        'url = "https://example.test/mcp"\n',
        encoding="utf-8",
    )
    agentstack_env = home / ".agentstack" / "env.sh"
    agentstack_env.parent.mkdir(parents=True)
    agentstack_env.write_text(
        f"export AGENTSTACK_MAIL_DB={old_db}\n"
        f"export AGENTSTACK_MAIL_ENV={old_env}\n"
        f"export AGENTSTACK_MAIL_HOME={old_home}\n"
        f"export AGENTSTACK_SIGNALS_DIR={old_signals}\n"
        f"export AGENTSTACK_MCP_URL={old_url}\n"
        "export AGENTSTACK_PROJECT_KEY=/project\n",
        encoding="utf-8",
    )
    agentstack_state = home / ".agentstack" / "install-state.json"
    _json(
        agentstack_state,
        {
            "env": {
                "AGENTSTACK_MAIL_DB": old_db,
                "AGENTSTACK_MAIL_ENV": old_env,
                "AGENTSTACK_MAIL_HOME": old_home,
                "AGENTSTACK_SIGNALS_DIR": old_signals,
                "AGENTSTACK_MCP_URL": old_url,
                "UNRELATED": "keep",
            },
            "retained_paths": [old_home],
            "purge_paths": [
                str(home / ".agentstack" / "runtime"),
                old_home,
                str(Path(old_db).parent),
            ],
        },
        newline=True,
    )
    app_root = home / ".agentstack" / "integrations" / "codex_app"
    app_root.mkdir(parents=True)
    app_env = app_root / "env.sh"
    app_env.write_text(
        f"export AGENTSTACK_MCP_URL={old_url}\n"
        f"export AGENTSTACK_MAIL_ENV={old_env}\n"
        f"export AGENTSTACK_SIGNALS_DIR={old_signals}\n"
        "export AGENTSTACK_CODEX_APP_PLUGIN_ID=agentstack\n",
        encoding="utf-8",
    )
    app_state = app_root / "install-state.json"
    _json(
        app_state,
        {
            "agent_mail_url": old_url,
            "agent_mail_env": old_env,
            "signals_dir": old_signals,
            "plugin_server_key": "agentstack",
        },
        newline=True,
    )
    claude_child = home / ".agentstack" / "runtime" / "child.mcp.json"
    _json(
        claude_child,
        {
            "mcpServers": {
                "mcp-agent-mail": {
                    "command": "/bin/runner",
                    "args": [],
                    "env": {
                        "AGENTSTACK_MCP_URL": old_url,
                        "AGENTSTACK_MAIL_ENV": old_env,
                        "AGENTSTACK_PROXY_AGENT_NAME": "Child",
                    },
                }
            }
        },
        newline=False,
    )
    codex_child = home / ".agentstack" / "runtime" / "child.codex-home" / "config.toml"
    codex_child.parent.mkdir(parents=True)
    codex_child.write_text(
        '[mcp_servers.agent-mail]\n'
        'command = "/bin/runner"\n'
        'args = []\n\n'
        '[mcp_servers.agent-mail.env]\n'
        f'AGENTSTACK_MCP_URL = "{old_url}"\n'
        f'AGENTSTACK_MAIL_ENV = "{old_env}"\n'
        'AGENTSTACK_PROXY_AGENT_NAME = "Child"\n',
        encoding="utf-8",
    )
    targets = [
        ("claude_mcp", claude),
        ("claude_settings", settings),
        ("claude_settings", local_settings),
        ("codex_mcp", codex),
        ("agentstack_env", agentstack_env),
        ("agentstack_state", agentstack_state),
        ("codex_app_env", app_env),
        ("codex_app_state", app_state),
        ("claude_child_mcp", claude_child),
        ("codex_child_mcp", codex_child),
    ]
    for _, path in targets:
        os.chmod(path, 0o600)
    inventory = tmp_path / "inventory.json"
    _json(
        inventory,
        {
            "schema_version": 1,
            "desired": {
                "legacy_mcp_url": old_url,
                "new_mcp_url": new_url,
                "legacy_mail_db": old_db,
                "new_mail_db": new_db,
                "legacy_mail_env": old_env,
                "new_mail_env": new_env,
                "legacy_mail_home": old_home,
                "new_mail_home": new_home,
                "legacy_signals_dir": old_signals,
                "new_signals_dir": new_signals,
            },
            "consumers": [
                {"kind": kind, "path": str(path)} for kind, path in targets
            ],
        },
        newline=True,
    )
    before = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for _, path in targets
    }
    return inventory, tmp_path / "bundle", before


def _digest(result: dict[str, object]) -> str:
    return str(result["manifest_sha256"])


def _set_client_keys(inventory: Path, *, claude: str, codex: str) -> None:
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["desired"]["claude_mcp_key"] = claude
    payload["desired"]["codex_mcp_key"] = codex
    _json(inventory, payload, newline=True)


def test_prepare_apply_and_one_operation_rollback(tmp_path: Path) -> None:
    inventory, bundle, before = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)

    assert prepared["status"] == "prepared"
    assert prepared["consumer_count"] == 10
    assert all(path.read_bytes() == payload for path, (payload, _) in before.items())
    assert stat.S_IMODE((bundle / "manifest.json").stat().st_mode) == 0o400
    planned = preview(bundle, _digest(prepared))
    assert planned["contents_redacted"] is True
    assert planned["files_changed"] == 8
    assert all(set(item) == {"path", "kind", "changed", "hunks"} for item in planned["changes"])

    digest = _digest(prepared)
    committed = apply(bundle, digest)
    assert committed["status"] == "committed"

    claude = json.loads(next(path for path in before if path.name == ".claude.json").read_text())
    assert "agentstack-mail" not in claude["mcpServers"]
    assert claude["mcpServers"]["mcp-agent-mail"] == {
        "type": "http",
        "url": "http://127.0.0.1:18765/mcp",
    }
    settings_path = next(path for path in before if path.name == "settings.json")
    settings = settings_path.read_text()
    assert settings_path.read_bytes() == before[settings_path][0]
    assert "mcp__mcp-agent-mail__register_agent" in settings
    assert "mcp__mcp-agent-mail__search_messages" in settings
    assert "mcp__mcp-agent-mail__summarize_thread" in settings
    local_settings_path = next(
        path for path in before if path.name == "settings.local.json"
    )
    assert local_settings_path.read_bytes() == before[local_settings_path][0]
    codex_path = next(path for path in before if path.name == "config.toml" and ".codex" in str(path))
    codex = codex_path.read_text()
    assert "[mcp_servers.agent-mail]" in codex
    assert "[mcp_servers.agentstack-mail]" not in codex
    assert "bearer_token_env_var" not in codex
    assert "[mcp_servers.agent-mail.tools.search_messages]" in codex
    assert "[mcp_servers.agent-mail.tools.summarize_thread]" in codex
    assert "# preserve this comment" in codex

    restored = _rollback(bundle, digest)
    assert restored["status"] == "rolled_back"
    for path, (payload, mode) in before.items():
        assert path.read_bytes() == payload
        assert stat.S_IMODE(path.stat().st_mode) == mode


def test_default_client_keys_are_independent_from_provider_identity(
    tmp_path: Path,
) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    desired = consumer.Desired.from_payload(
        json.loads(inventory.read_text(encoding="utf-8"))["desired"]
    )

    assert consumer.PROVIDER_IDENTITY == "agentstack-mail"
    assert desired.claude_mcp_key == "mcp-agent-mail"
    assert desired.codex_mcp_key == "agent-mail"
    assert consumer.PROVIDER_IDENTITY not in {
        desired.claude_mcp_key,
        desired.codex_mcp_key,
    }

    prepared = prepare(inventory, bundle)
    apply(bundle, _digest(prepared))
    claude_path = _target_from_inventory(inventory, "claude_mcp")
    codex_path = _target_from_inventory(inventory, "codex_mcp")
    assert set(json.loads(claude_path.read_text())["mcpServers"]) >= {
        "mcp-agent-mail"
    }
    assert "[mcp_servers.agent-mail]" in codex_path.read_text(encoding="utf-8")


def test_explicit_client_key_rename_remains_available(tmp_path: Path) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    _set_client_keys(
        inventory,
        claude="agentstack-mail",
        codex="agentstack-mail",
    )

    prepared = prepare(inventory, bundle)
    apply(bundle, _digest(prepared))

    claude_path = _target_from_inventory(inventory, "claude_mcp")
    claude = json.loads(claude_path.read_text(encoding="utf-8"))
    assert "mcp-agent-mail" not in claude["mcpServers"]
    assert claude["mcpServers"]["agentstack-mail"]["url"].endswith(":18765/mcp")
    settings_path = _target_from_inventory(inventory, "claude_settings")
    settings = settings_path.read_text(encoding="utf-8")
    assert "mcp__mcp-agent-mail__" not in settings
    assert "mcp__agentstack-mail__register_agent" in settings
    codex_path = _target_from_inventory(inventory, "codex_mcp")
    codex = codex_path.read_text(encoding="utf-8")
    assert "[mcp_servers.agentstack-mail]" in codex
    assert "[mcp_servers.agent-mail]" not in codex


def test_prepare_is_read_only_and_external_edit_aborts_apply(tmp_path: Path) -> None:
    inventory, bundle, before = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)
    changed = next(iter(before))
    changed.write_bytes(changed.read_bytes() + b"\n")
    snapshot = {path: path.read_bytes() for path in before}

    with pytest.raises(ConsumerError, match="diverged"):
        apply(bundle, _digest(prepared))

    assert {path: path.read_bytes() for path in before} == snapshot
    assert status(bundle, _digest(prepared))["status"] == "incident"


@pytest.mark.parametrize("replace_index", [0, 4, 9])
def test_crash_after_replacements_is_uncommitted_and_one_rollback_recovers(
    tmp_path: Path, replace_index: int
) -> None:
    inventory, bundle, before = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)

    def crash(phase: str) -> None:
        if phase == f"after_replace:{replace_index}":
            raise OSError("simulated crash")

    with pytest.raises(OSError, match="simulated crash"):
        apply(bundle, _digest(prepared), fault_hook=crash)
    assert status(bundle, _digest(prepared))["status"] in {
        "mixed_uncommitted",
        "all_after_uncommitted",
    }

    assert _rollback(bundle, _digest(prepared))["status"] == "rolled_back"
    assert all(path.read_bytes() == payload for path, (payload, _) in before.items())


def test_publish_marker_without_replacement_is_not_reported_prepared(
    tmp_path: Path,
) -> None:
    inventory, bundle, before = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)

    def crash(phase: str) -> None:
        if phase == "after_journal:PUBLISHING":
            raise OSError("simulated crash")

    with pytest.raises(OSError, match="simulated crash"):
        apply(bundle, _digest(prepared), fault_hook=crash)
    assert status(bundle, _digest(prepared))["status"] == "all_before_uncommitted"
    assert _rollback(bundle, _digest(prepared))["status"] == "rolled_back"
    assert all(path.read_bytes() == payload for path, (payload, _) in before.items())


def test_external_edit_after_commit_blocks_rollback_without_more_writes(tmp_path: Path) -> None:
    inventory, bundle, before = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)
    apply(bundle, _digest(prepared))
    changed = next(iter(before))
    changed.write_bytes(changed.read_bytes() + b"external")
    snapshot = {path: path.read_bytes() for path in before}

    with pytest.raises(ConsumerError, match="diverged"):
        _rollback(bundle, _digest(prepared))

    assert {path: path.read_bytes() for path in before} == snapshot


@pytest.mark.parametrize("replace_index", [0, 5, 9])
def test_interrupted_rollback_resumes_with_one_rollback_command(
    tmp_path: Path, replace_index: int
) -> None:
    inventory, bundle, before = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)
    apply(bundle, _digest(prepared))

    def crash(phase: str) -> None:
        if phase == f"after_replace:{replace_index}":
            raise OSError("simulated rollback crash")

    with pytest.raises(OSError, match="rollback crash"):
        _rollback(bundle, _digest(prepared), fault_hook=crash)
    assert status(bundle, _digest(prepared))["status"] in {
        "mixed_uncommitted",
        "all_before_uncommitted",
    }
    assert _rollback(bundle, _digest(prepared))["status"] == "rolled_back"
    assert all(path.read_bytes() == payload for path, (payload, _) in before.items())


def test_apply_and_rollback_are_idempotent_for_exact_vectors(tmp_path: Path) -> None:
    inventory, bundle, before = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)
    digest = _digest(prepared)
    assert apply(bundle, digest)["status"] == "committed"
    after = {path: path.read_bytes() for path in before}
    assert apply(bundle, digest)["status"] == "committed"
    assert {path: path.read_bytes() for path in before} == after
    assert _rollback(bundle, digest)["status"] == "rolled_back"
    assert _rollback(bundle, digest)["status"] == "rolled_back"
    assert all(path.read_bytes() == payload for path, (payload, _) in before.items())


def test_fake_committed_journal_cannot_override_the_actual_vector(tmp_path: Path) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)
    journal = json.loads((bundle / "journal.json").read_text())
    journal["phase"] = "COMMITTED"
    (bundle / "journal.json").write_text(json.dumps(journal) + "\n")
    result = status(bundle, _digest(prepared))
    assert result["journal_phase"] == "COMMITTED"
    assert result["status"] == "all_before_uncommitted"


def test_manifest_and_blob_tamper_are_rejected(tmp_path: Path) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)
    digest = _digest(prepared)
    os.chmod(bundle / "manifest.json", 0o600)
    (bundle / "manifest.json").write_bytes((bundle / "manifest.json").read_bytes() + b" ")
    os.chmod(bundle / "manifest.json", 0o400)
    with pytest.raises(ConsumerError, match="external pin"):
        apply(bundle, digest)

    inventory2, bundle2, _ = _fixture(tmp_path / "second")
    prepared2 = prepare(inventory2, bundle2)
    os.chmod(bundle2 / "0000.after", 0o600)
    (bundle2 / "0000.after").write_bytes(b"tampered")
    os.chmod(bundle2 / "0000.after", 0o400)
    with pytest.raises(ConsumerError, match="failed digest"):
        apply(bundle2, _digest(prepared2))


def test_duplicate_json_key_and_hardlink_fail_before_bundle_creation(tmp_path: Path) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    payload = json.loads(inventory.read_text())
    target = Path(payload["consumers"][0]["path"])
    target.write_text('{"mcpServers":{},"mcpServers":{}}', encoding="utf-8")
    with pytest.raises(ConsumerError, match="duplicate JSON key"):
        prepare(inventory, bundle)
    assert not bundle.exists()

    inventory2, bundle2, _ = _fixture(tmp_path / "second")
    payload2 = json.loads(inventory2.read_text())
    target2 = Path(payload2["consumers"][0]["path"])
    alias = target2.with_name("claude-hardlink.json")
    os.link(target2, alias)
    with pytest.raises(ConsumerError, match="hard links"):
        prepare(inventory2, bundle2)


def test_cli_requires_external_digest_and_reports_prepared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    main(["prepare", "--inventory", str(inventory), "--bundle", str(bundle)])
    prepared = json.loads(capsys.readouterr().out)
    main(
        [
            "status",
            "--bundle",
            str(bundle),
            "--expected-manifest-sha256",
            prepared["manifest_sha256"],
        ]
    )
    assert json.loads(capsys.readouterr().out)["status"] == "prepared"


def _target_from_inventory(inventory: Path, kind: str, *, occurrence: int = 0) -> Path:
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    matches = [
        Path(item["path"])
        for item in payload["consumers"]
        if item["kind"] == kind
    ]
    return matches[occurrence]


def _migration_baseline(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    legacy = tmp_path / "legacy-authority"
    legacy.mkdir()
    database = legacy / "storage.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE projects (
          id INTEGER PRIMARY KEY, slug TEXT NOT NULL, human_key TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE agents (
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
          name TEXT NOT NULL, program TEXT NOT NULL, model TEXT NOT NULL,
          task_description TEXT NOT NULL, inception_ts TEXT NOT NULL,
          last_active_ts TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
          sender_id INTEGER NOT NULL, thread_id TEXT, subject TEXT NOT NULL,
          body_md TEXT NOT NULL, importance TEXT NOT NULL,
          ack_required INTEGER NOT NULL, created_ts TEXT NOT NULL,
          attachments TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id),
          FOREIGN KEY(sender_id) REFERENCES agents(id)
        );
        CREATE TABLE message_recipients (
          message_id INTEGER NOT NULL, agent_id INTEGER NOT NULL,
          kind TEXT NOT NULL, read_ts TEXT, ack_ts TEXT,
          PRIMARY KEY(message_id, agent_id),
          FOREIGN KEY(message_id) REFERENCES messages(id),
          FOREIGN KEY(agent_id) REFERENCES agents(id)
        );
        CREATE TABLE file_reservations (
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
          agent_id INTEGER NOT NULL, path_pattern TEXT NOT NULL,
          exclusive INTEGER NOT NULL, reason TEXT NOT NULL,
          created_ts TEXT NOT NULL, expires_ts TEXT NOT NULL, released_ts TEXT,
          FOREIGN KEY(project_id) REFERENCES projects(id),
          FOREIGN KEY(agent_id) REFERENCES agents(id)
        );
        INSERT INTO projects VALUES (1, 'project', '/tmp/project', '2026-08-10T00:00:00');
        INSERT INTO agents VALUES
          (10, 1, 'ProOpus', 'claude-code', 'opus', '',
           '2026-08-10T00:00:00', '2026-08-10T00:00:00');
        INSERT INTO messages VALUES
          (20, 1, 10, NULL, 'subject', 'body', 'normal', 0,
           '2026-08-10T00:01:00', '[]');
        INSERT INTO message_recipients VALUES (20, 10, 'to', NULL, NULL);
        INSERT INTO file_reservations VALUES
          (30, 1, 10, 'src/**', 1, 'fixture', '2026-08-10T00:02:00',
           '2026-08-10T01:02:00', NULL);
        """
    )
    connection.commit()
    connection.close()
    archive = legacy / "archive"
    archive.mkdir()
    (archive / "message.md").write_text("message 20\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(archive)], check=True)
    subprocess.run(
        ["git", "-C", str(archive), "config", "user.name", "Consumer Test"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(archive),
            "config",
            "user.email",
            "consumer@example.test",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(archive), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(archive), "commit", "-q", "-m", "fixture"],
        check=True,
    )
    signals = legacy / "signals"
    signals.mkdir()
    (signals / "20.signal").write_text('{"message_id":20}\n', encoding="utf-8")
    destination = tmp_path / "new-authority"
    copy_state(StatePaths.from_root(legacy), destination)
    runtime = destination / "runtime"
    runtime.mkdir(mode=0o700)
    (runtime / "authority.lock").touch(mode=0o600)
    return destination / MIGRATION_MANIFEST_NAME, destination


def test_terminal_receipt_not_mutable_journal_authorizes_commit(tmp_path: Path) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)

    def stop_after_last_target(phase: str) -> None:
        if phase == "after_parent_fsync:9":
            raise OSError("receipt not written")

    with pytest.raises(OSError, match="receipt not written"):
        apply(bundle, _digest(prepared), fault_hook=stop_after_last_target)
    assert status(bundle, _digest(prepared))["status"] == "all_after_uncommitted"
    journal = json.loads((bundle / "journal.json").read_text(encoding="utf-8"))
    journal["phase"] = "COMMITTED"
    (bundle / "journal.json").write_text(json.dumps(journal) + "\n", encoding="utf-8")
    assert status(bundle, _digest(prepared))["status"] == "all_after_uncommitted"

    committed = apply(bundle, _digest(prepared))
    assert committed["status"] == "committed"
    assert committed["committed_receipt"] is True
    assert stat.S_IMODE((bundle / "committed.json").stat().st_mode) == 0o400


def test_invalid_terminal_receipt_is_an_incident(tmp_path: Path) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)
    apply(bundle, _digest(prepared))
    os.chmod(bundle / "committed.json", 0o600)

    result = status(bundle, _digest(prepared))
    assert result["status"] == "incident"
    assert result["receipt_invalid"] is True


def test_rollback_rechecks_authority_after_staging_before_any_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, bundle, before = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)
    apply(bundle, _digest(prepared))
    committed = {path: path.read_bytes() for path in before}
    calls = 0

    def changing_authority(*_: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConsumerError("new authority received a durable write")

    monkeypatch.setattr(consumer, "_assert_data_reversible", changing_authority)
    with pytest.raises(ConsumerError, match="durable write"):
        rollback(
            bundle,
            _digest(prepared),
            bundle / "migration-manifest.json",
            "C5_CLIENT_SWITCHING",
        )

    assert calls == 2
    assert {path: path.read_bytes() for path in before} == committed
    assert status(bundle, _digest(prepared))["status"] == "committed"


def test_migration_assessment_no_go_reason_is_exposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        consumer,
        "assess_rollback",
        lambda *_: {
            "status": "no_go",
            "data_reversible": False,
            "reason": "new authority diverged after baseline",
        },
    )
    with pytest.raises(ConsumerError, match="diverged after baseline"):
        _REAL_ASSERT_DATA_REVERSIBLE(
            tmp_path / "migration-manifest.json", "C5_CLIENT_SWITCHING"
        )


def test_real_migration_baseline_gates_success_and_post_baseline_no_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration_manifest, destination = _migration_baseline(tmp_path / "authority")
    inventory, bundle, before = _fixture(tmp_path / "consumers")
    prepared = prepare(inventory, bundle)
    digest = _digest(prepared)
    apply(bundle, digest)
    monkeypatch.setattr(
        consumer, "_assert_data_reversible", _REAL_ASSERT_DATA_REVERSIBLE
    )
    monkeypatch.setattr(
        consumer, "_with_authority_lock", _REAL_WITH_AUTHORITY_LOCK
    )

    assert rollback(
        bundle,
        digest,
        migration_manifest,
        "C5_CLIENT_SWITCHING",
    )["status"] == "rolled_back"
    assert all(path.read_bytes() == payload for path, (payload, _) in before.items())

    apply(bundle, digest)
    committed = {path: path.read_bytes() for path in before}
    (destination / "signals" / "post-baseline.signal").write_text(
        "durable write\n", encoding="utf-8"
    )
    with pytest.raises(ConsumerError, match="no-go|no_go|diverged"):
        rollback(
            bundle,
            digest,
            migration_manifest,
            "C5_CLIENT_SWITCHING",
        )
    assert {path: path.read_bytes() for path in before} == committed
    assert status(bundle, digest)["status"] == "committed"


def test_real_authority_lock_and_manifest_status_fail_before_config_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration_manifest, destination = _migration_baseline(tmp_path / "authority")
    inventory, bundle, before = _fixture(tmp_path / "consumers")
    prepared = prepare(inventory, bundle)
    digest = _digest(prepared)
    apply(bundle, digest)
    committed = {path: path.read_bytes() for path in before}
    monkeypatch.setattr(
        consumer, "_assert_data_reversible", _REAL_ASSERT_DATA_REVERSIBLE
    )
    monkeypatch.setattr(
        consumer, "_with_authority_lock", _REAL_WITH_AUTHORITY_LOCK
    )

    lock = (destination / "runtime" / "authority.lock").open("r+b")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ConsumerError, match="still owns the authority lock"):
            rollback(
                bundle,
                digest,
                migration_manifest,
                "C5_CLIENT_SWITCHING",
            )
    finally:
        lock.close()
    assert {path: path.read_bytes() for path in before} == committed

    manifest = json.loads(migration_manifest.read_text(encoding="utf-8"))
    manifest["status"] = "C2_LEGACY_QUIESCED"
    migration_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ConsumerError, match="verified C3 baseline"):
        rollback(
            bundle,
            digest,
            migration_manifest,
            "C5_CLIENT_SWITCHING",
        )
    assert {path: path.read_bytes() for path in before} == committed


def test_target_parent_lock_serializes_distinct_bundles(tmp_path: Path) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    first = prepare(inventory, bundle)
    second_bundle = tmp_path / "second-bundle"
    second = prepare(inventory, second_bundle)
    _, entries = consumer._load_bundle(bundle, _digest(first))
    script = (
        "from pathlib import Path; from agentstack_mail.consumer import apply; "
        "apply(Path(__import__('sys').argv[1]), __import__('sys').argv[2])"
    )
    with consumer._with_target_locks(entries):
        blocked = subprocess.run(
            [sys.executable, "-c", script, str(second_bundle), _digest(second)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    assert blocked.returncode != 0
    assert "target-directory lock" in blocked.stderr
    assert apply(second_bundle, _digest(second))["status"] == "committed"


def test_rollback_cli_requires_manifest_and_assessable_stage(
    tmp_path: Path,
) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)
    base = [
        "rollback",
        "--bundle",
        str(bundle),
        "--expected-manifest-sha256",
        _digest(prepared),
    ]
    with pytest.raises(SystemExit) as missing:
        main(base)
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as invalid:
        main(
            base
            + [
                "--migration-manifest",
                str(bundle / "migration-manifest.json"),
                "--cutover-stage",
                "C2_LEGACY_QUIESCED",
            ]
        )
    assert invalid.value.code == 2


def test_xattrs_survive_apply_and_rollback(tmp_path: Path) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    target = _target_from_inventory(inventory, "claude_mcp")
    name = "com.agentstack.test" if sys.platform == "darwin" else "user.agentstack-test"
    try:
        if hasattr(os, "setxattr"):
            os.setxattr(target, name, b"preserve-me", follow_symlinks=False)
        elif Path("/usr/bin/xattr").is_file():
            subprocess.run(
                ["/usr/bin/xattr", "-wx", name, b"preserve-me".hex(), str(target)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:
            pytest.skip("platform has no xattr writer")
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"test filesystem does not support xattrs: {exc}")
    prepared = prepare(inventory, bundle)
    apply(bundle, _digest(prepared))
    if hasattr(os, "getxattr"):
        read_xattr = lambda: os.getxattr(target, name, follow_symlinks=False)
    else:
        read_xattr = lambda: bytes.fromhex(
            subprocess.run(
                ["/usr/bin/xattr", "-px", name, str(target)],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
        )
    assert read_xattr() == b"preserve-me"
    _rollback(bundle, _digest(prepared))
    assert read_xattr() == b"preserve-me"


def test_hard_process_exit_reuses_stages_and_leaves_no_debris(tmp_path: Path) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("requires fork to simulate an uncatchable process exit")
    inventory, bundle, _ = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions run in the parent.
        def terminate(phase: str) -> None:
            if phase == "after_stage:4":
                os._exit(91)

        apply(bundle, _digest(prepared), fault_hook=terminate)
        os._exit(92)
    _, wait_status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(wait_status) == 91
    assert list(tmp_path.rglob(".agentstack-consumer.*.ready"))

    assert apply(bundle, _digest(prepared))["status"] == "committed"
    assert not list(tmp_path.rglob(".agentstack-consumer.*.ready"))
    assert not list(tmp_path.rglob(".agentstack-consumer.*.building"))


def test_partial_building_stage_is_rebuilt_but_unsafe_ready_is_rejected(
    tmp_path: Path,
) -> None:
    inventory, bundle, before = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["entries"][0]
    building = consumer._stage_path(
        entry,
        operation_id=manifest["operation_id"],
        index=0,
        field="after",
        state="building",
    )
    building.write_bytes(b"partial crash debris")
    assert apply(bundle, _digest(prepared))["status"] == "committed"
    _rollback(bundle, _digest(prepared))

    inventory2, bundle2, before2 = _fixture(tmp_path / "second")
    prepared2 = prepare(inventory2, bundle2)
    manifest2 = json.loads((bundle2 / "manifest.json").read_text(encoding="utf-8"))
    entry2 = manifest2["entries"][0]
    ready = consumer._stage_path(
        entry2,
        operation_id=manifest2["operation_id"],
        index=0,
        field="after",
    )
    ready.symlink_to(bundle2 / entry2["after_blob"])
    snapshot = {path: path.read_bytes() for path in before2}
    with pytest.raises(ConsumerError, match="symlink|regular file"):
        apply(bundle2, _digest(prepared2))
    assert {path: path.read_bytes() for path in before2} == snapshot
    assert all(path.read_bytes() == payload for path, (payload, _) in before.items())


def test_before_replace_race_is_detected_before_first_canonical_write(
    tmp_path: Path,
) -> None:
    inventory, bundle, before = _fixture(tmp_path)
    prepared = prepare(inventory, bundle)
    changed = next(iter(before))

    def external_writer(phase: str) -> None:
        if phase == "before_replace:0":
            changed.write_bytes(changed.read_bytes() + b"external")

    with pytest.raises(ConsumerError, match="changed before replacement"):
        apply(bundle, _digest(prepared), fault_hook=external_writer)
    assert changed.read_bytes().endswith(b"external")
    for path, (payload, _) in list(before.items())[1:]:
        assert path.read_bytes() == payload


def test_child_proxy_preserves_tool_settings_while_endpoint_changes(tmp_path: Path) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    path = _target_from_inventory(inventory, "codex_child_mcp")
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n[mcp_servers.agent-mail.tools.bootstrap]\napproval_mode = \"never\"\n"
        + "\n[mcp_servers.agent-mail.tools.fetch_inbox]\napproval_mode = \"never\"\n"
        + "\n[mcp_servers.agent-mail.tools.health_check]\napproval_mode = \"never\"\n",
        encoding="utf-8",
    )
    prepared = prepare(inventory, bundle)
    apply(bundle, _digest(prepared))
    rendered = path.read_text(encoding="utf-8")
    assert "tools.bootstrap" in rendered
    assert "tools.fetch_inbox" in rendered
    assert "tools.health_check" in rendered
    assert "[mcp_servers.agent-mail" in rendered
    assert "[mcp_servers.agentstack-mail" not in rendered


def test_already_new_claude_child_is_idempotent(tmp_path: Path) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    desired = payload["desired"]
    path = _target_from_inventory(inventory, "claude_child_mcp")
    value = json.loads(path.read_text(encoding="utf-8"))
    entry = value["mcpServers"].pop("mcp-agent-mail")
    entry["env"]["AGENTSTACK_MCP_URL"] = desired["new_mcp_url"]
    entry["env"]["AGENTSTACK_MAIL_ENV"] = desired["new_mail_env"]
    value["mcpServers"]["agentstack-mail"] = entry
    _json(path, value, newline=False)

    prepared = prepare(inventory, bundle)
    assert apply(bundle, _digest(prepared))["status"] == "committed"
    rendered = json.loads(path.read_text())["mcpServers"]
    assert "mcp-agent-mail" in rendered
    assert "agentstack-mail" not in rendered


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("child_missing_env", "lacks explicit endpoint"),
        ("loopback_alias", "undeclared Claude authority endpoint alias"),
        ("codex_mixed_transport", "mixed or unknown transport"),
        ("codex_unknown_bearer", "unknown bearer selector"),
        ("new_selector_only_in_note", "no declared supported tool selector"),
        ("invalid_new_db", "new_mail_db must be"),
    ],
)
def test_ambiguous_or_incomplete_consumer_inputs_fail_closed(
    tmp_path: Path, case: str, error: str
) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    if case == "child_missing_env":
        path = _target_from_inventory(inventory, "codex_child_mcp")
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'AGENTSTACK_MAIL_ENV = "', 'REMOVED_MAIL_ENV = "'
            ),
            encoding="utf-8",
        )
    elif case == "loopback_alias":
        path = _target_from_inventory(inventory, "claude_mcp")
        value = json.loads(path.read_text(encoding="utf-8"))
        value["mcpServers"]["shadow"] = {
            "type": "http",
            "url": "http://localhost:8765/mcp",
        }
        _json(path, value, newline=False)
    elif case == "codex_mixed_transport":
        path = _target_from_inventory(inventory, "codex_mcp")
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'url = "http://127.0.0.1:8765/api/"',
                'url = "http://127.0.0.1:8765/api/"\ncommand = "/bin/false"',
            ),
            encoding="utf-8",
        )
    elif case == "codex_unknown_bearer":
        path = _target_from_inventory(inventory, "codex_mcp")
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "MCP_AGENT_MAIL_TOKEN", "UNKNOWN_TOKEN"
            ),
            encoding="utf-8",
        )
    elif case == "new_selector_only_in_note":
        path = _target_from_inventory(inventory, "claude_settings")
        _json(
            path,
            {
                "note": "mcp__agentstack-mail__fetch_inbox",
                "permissions": {"allow": ["Bash(git status:*)"]},
            },
            newline=True,
        )
    else:
        payload["desired"]["new_mail_db"] = str(tmp_path / "elsewhere.sqlite3")
        _json(inventory, payload, newline=True)
    with pytest.raises(ConsumerError, match=error):
        prepare(inventory, bundle)
    assert not bundle.exists()


def test_install_receipt_cannot_purge_cold_legacy_roots(tmp_path: Path) -> None:
    inventory, bundle, _ = _fixture(tmp_path)
    desired = json.loads(inventory.read_text(encoding="utf-8"))["desired"]
    state_path = _target_from_inventory(inventory, "agentstack_state")
    prepared = prepare(inventory, bundle)
    apply(bundle, _digest(prepared))
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert desired["legacy_mail_home"] not in state["purge_paths"]
    assert str(Path(desired["legacy_mail_db"]).parent) not in state["purge_paths"]
    assert desired["new_mail_home"] in state["purge_paths"]
    assert desired["legacy_mail_home"] in state["retained_paths"]
    assert str(Path(desired["legacy_mail_db"]).parent) in state["retained_paths"]
