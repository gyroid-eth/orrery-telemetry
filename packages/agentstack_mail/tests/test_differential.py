"""Hermetic frozen-live versus AgentStack Mail behavior differential.

Absolute clock values and archive filename timestamps are intentionally
different because the two isolated workers run sequentially.  The oracle
validates each raw artifact first, then normalizes only those nondeterministic
representations before requiring exact equality of tool results and durable
state after every checkpoint.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from agentstack_mail.contract import COMPATIBILITY_TOOLS
from differential_source import (
    CORE_NAMESPACE,
    LIVE_NAMESPACE,
    WorkerStateRoots,
    isolated_worker_env,
    reconstruct_live,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
CORE_SOURCE = PACKAGE_ROOT / "src"
PROBE = TESTS_ROOT / "differential_probe.py"
EXPECTED_DIVERGENCES = (
    PACKAGE_ROOT / "fixtures" / "differential-expected-divergences-v1.json"
)

_SCENARIOS = ("identity", "lifecycle", "reservation_signal")
_RICH_TIMING_PANEL_SCENARIOS = frozenset({"identity", "reservation_signal"})
_EXPECTED_EVENTS = {
    "identity": (
        "01_ensure_project",
        "02_register_green",
        "03_register_blue",
        "04_reregister_green_same_token",
        "05_whois_green",
        "06_blue_contacts_only",
        "07_request_contact",
        "08_list_contacts_pending",
        "09_respond_contact_accept",
        "10_list_contacts_approved",
        "11_send_message",
        "12_fetch_blue_inbox",
        "13_fetch_topic",
        "14_mark_message_read",
        "15_acknowledge_message",
        "16_acknowledge_message_replay",
        "17_reply_message",
        "18_fetch_green_inbox",
    ),
    "lifecycle": (
        "01_health_checked",
        "02_project_ensured",
        "03_agent_registered_GreenCastle",
        "04_agent_registered_BlueLake",
        "05_session_started_with_reservation",
        "06_reservation_cycle_auto_released",
        "07_contact_handshake_auto_accepted",
        "08_empty_summary_collection_fetched",
        "09_peer_retired",
    ),
    "reservation_signal": (
        "01_project_ensured",
        "02_agent_registered_GreenCastle",
        "03_agent_registered_BlueLake",
        "04_agent_registered_RedStone",
        "05_contact_policy_open_BlueLake",
        "06_contact_policy_open_RedStone",
        "07_reservation_created_nfd",
        "08_reservation_reacquired_nfc_same_agent",
        "09_reservation_conflict_other_agent",
        "10_reservation_renewed_by_overlap",
        "11_reservation_released_by_overlap",
        "12_reservation_acquired_after_release",
        "13_message_sent_first",
        "14_message_sent_second",
        "15_blue_inbox_fetched",
    ),
}
_EXPECTED_REGISTERED_AGENTS = {
    "identity": frozenset({"GreenCastle", "BlueLake"}),
    "lifecycle": frozenset({"GreenCastle", "BlueLake"}),
    "reservation_signal": frozenset({"GreenCastle", "BlueLake", "RedStone"}),
}
_EXPECTED_TOOL_TRACE = {
    "identity": (
        "ensure_project",
        "register_agent",
        "register_agent",
        "register_agent",
        "whois",
        "set_contact_policy",
        "request_contact",
        "list_contacts",
        "respond_contact",
        "list_contacts",
        "send_message",
        "fetch_inbox",
        "fetch_topic",
        "mark_message_read",
        "acknowledge_message",
        "acknowledge_message",
        "reply_message",
        "fetch_inbox",
    ),
    "lifecycle": (
        "health_check",
        "ensure_project",
        "register_agent",
        "register_agent",
        "macro_start_session",
        "macro_file_reservation_cycle",
        "macro_contact_handshake",
        "fetch_summary",
        "retire_agent",
    ),
    "reservation_signal": (
        "ensure_project",
        "register_agent",
        "register_agent",
        "register_agent",
        "set_contact_policy",
        "set_contact_policy",
        "file_reservation_paths",
        "file_reservation_paths",
        "file_reservation_paths",
        "renew_file_reservations",
        "release_file_reservations",
        "file_reservation_paths",
        "send_message",
        "send_message",
        "fetch_inbox",
    ),
}
_EXPECTED_DATABASE_TABLES = frozenset(
    {
        "projects",
        "agents",
        "agent_links",
        "messages",
        "message_recipients",
        "file_reservations",
    }
)
_EXPECTED_FINAL_GIT_COMMITS = {
    "identity": 7,
    "lifecycle": 7,
    "reservation_signal": 11,
}

_ARCHIVE_TIME_RE = re.compile(
    r"(?<!\d)\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z(?!\d)"
)
_DATETIME_RE = re.compile(
    r"(?<!\d)(\d{4}-\d{2}-\d{2})[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?(?!\d)"
)
_RICH_TOOL_CALL_PANEL_RE = re.compile(
    r"^╔═+ ✅ MCP TOOL CALL COMPLETED ═+╗ .* "
    r"╚═+ (?:⚡ Lightning Fast!|✓ Fast|Completed) ═+╝$",
    re.MULTILINE,
)
_RICH_DURATION_RE = re.compile(
    r"(⏱ Duration\s+│ )(?:⚡|⏱|🐌) \d+(?:\.\d+)?ms\s+(║)"
)
_RICH_COMPLETION_FOOTER_RE = re.compile(
    r"╚═+ (?:⚡ Lightning Fast!|✓ Fast|Completed) ═+╝"
)
_RICH_TIMING_LOG_PATH = ("durable", "git", "log")
_ARCHIVE_DATE_PATH_RE = re.compile(r"/(\d{4})/(\d{2})/<TIME:FILE_Z>")
_RAW_ARCHIVE_PATH_RE = re.compile(
    r"/(?P<directory_year>\d{4})/(?P<directory_month>\d{2})/"
    r"(?P<filename_time>(?P<filename_year>\d{4})-"
    r"(?P<filename_month>\d{2})-\d{2}T\d{2}-\d{2}-\d{2}Z)__"
)
_ARCHIVE_CREATED_RE = re.compile(r'"created":\s*"([^"]+)"')
_ARCHIVE_MESSAGE_ID_RE = re.compile(r"__(\d+)\.md$")
_THREAD_ENTRY_RE = re.compile(
    r"^## ([^\n]+) — [^\n]+\n\n\[View canonical\]\(([^)]+)\)",
    re.MULTILINE,
)


def _normalize_rich_timing_presentation(value: str) -> tuple[str, int]:
    def normalize_panel(match: re.Match[str]) -> str:
        panel, duration_replacements = _RICH_DURATION_RE.subn(
            r"\1<DURATION:PRESENTATION> \2",
            match.group(0),
        )
        panel, footer_replacements = _RICH_COMPLETION_FOOTER_RE.subn(
            "╚<COMPLETION:PRESENTATION>╝",
            panel,
        )
        if duration_replacements != 1 or footer_replacements != 1:
            raise AssertionError(
                "recognized Rich tool-call panel did not contain exactly one "
                "duration row and completion footer"
            )
        return panel

    return _RICH_TOOL_CALL_PANEL_RE.subn(normalize_panel, value)


@pytest.fixture(scope="session")
def frozen_live_checkout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return reconstruct_live(
        PACKAGE_ROOT,
        tmp_path_factory.mktemp("agentstack-mail-frozen-live"),
    )


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            yield from _walk_strings(item)


def _scenario_tools(scenario: str) -> frozenset[str]:
    module = __import__(f"differential_{scenario}")
    return frozenset(module.SCENARIO_TOOLS)


def _worker_payload(
    roots: WorkerStateRoots,
    project_key: Path,
    caller_tokens: Mapping[str, str],
    source_root: Path,
) -> dict[str, Any]:
    return {
        "version": 1,
        "state": {
            "state_root": str(roots.home.parent),
            "project_key": str(project_key),
            "database_path": str(roots.database),
            "archive_root": str(roots.storage),
            "signals_root": str(roots.signals),
            "source_root": str(source_root),
            "scenario_root": str(TESTS_ROOT),
        },
        "secrets": dict(caller_tokens),
    }


def _run_worker(
    *,
    namespace: str,
    scenario: str,
    roots: WorkerStateRoots,
    project_key: Path,
    caller_tokens: Mapping[str, str],
    source_root: Path,
) -> dict[str, Any]:
    input_path = roots.home.parent / "input.json"
    output_path = roots.home.parent / "output.json"
    environment = isolated_worker_env(os.environ, namespace, roots)
    encoded_input = json.dumps(
        _worker_payload(roots, project_key, caller_tokens, source_root),
        sort_keys=True,
    ).encode("utf-8")
    descriptor = os.open(
        input_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(encoded_input)
    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--namespace",
            namespace,
            "--scenario",
            scenario,
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ],
        cwd=roots.cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=240,
        check=False,
    )
    transcript = completed.stdout + completed.stderr
    if any(token in transcript for token in caller_tokens.values()):
        pytest.fail(
            f"{namespace} {scenario} worker disclosed a caller token in its transcript",
            pytrace=False,
        )
    if completed.returncode != 0:
        diagnostic = transcript[-4000:]
        pytest.fail(
            f"{namespace} {scenario} worker exited {completed.returncode}:\n{diagnostic}",
            pytrace=False,
        )
    if not output_path.is_file():
        pytest.fail(
            f"{namespace} {scenario} worker did not create its output",
            pytrace=False,
        )
    assert output_path.stat().st_mode & 0o077 == 0
    output = json.loads(output_path.read_text(encoding="utf-8"))
    serialized = json.dumps(output, sort_keys=True, ensure_ascii=False)
    if any(token in serialized for token in caller_tokens.values()):
        pytest.fail(
            f"{namespace} {scenario} output disclosed a caller token",
            pytrace=False,
        )
    assert "<UNEXPECTED_TOKEN_VALUE>" not in serialized
    assert "<SERVER_GENERATED_TOKEN>" not in serialized
    return output


def _expected_topology(side: str) -> dict[str, Any]:
    manifest = json.loads(EXPECTED_DIVERGENCES.read_text(encoding="utf-8"))
    intentional = manifest["intentional_differences"]
    summary = intentional["server_topology"][side]
    surface = next(
        entry
        for entry in intentional["allowlisted_entries"]
        if entry["id"] == "topology.publication_surface"
    )[side]
    for count_name in (
        "tool_count",
        "resource_count",
        "resource_template_count",
        "prompt_count",
    ):
        assert surface[count_name] == summary[count_name]
    return surface


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _assert_contact_ttl(checkpoints: Sequence[Mapping[str, Any]]) -> None:
    tables = checkpoints[-1]["durable"]["database"]["tables"]
    links = tables["agent_links"]
    assert len(links) == 1
    assert links[0]["status"] == "approved"
    updated = _parse_time(links[0]["updated_ts"])
    expires = _parse_time(links[0]["expires_ts"])
    assert (expires - updated).total_seconds() == 604800


def _assert_archive_derivation(durable: Mapping[str, Any]) -> None:
    files = durable["archive"]["files"]
    dated_copies: dict[str, list[str]] = {}
    for path, artifact in files.items():
        match = _RAW_ARCHIVE_PATH_RE.search(path)
        if not match:
            continue
        text = artifact["text"]
        if not text.startswith("---json\n") or "\n---\n" not in text:
            raise AssertionError(f"dated archive record lacks JSON frontmatter: {path}")
        frontmatter_text = text[len("---json\n") :].split("\n---\n", 1)[0]
        frontmatter = json.loads(frontmatter_text)
        created = _parse_time(frontmatter["created"])
        assert path.split("/")[-3:-1] == [
            created.strftime("%Y"),
            created.strftime("%m"),
        ]
        assert match.group("filename_time") == created.strftime(
            "%Y-%m-%dT%H-%M-%SZ"
        )
        id_match = _ARCHIVE_MESSAGE_ID_RE.search(path)
        assert id_match is not None
        assert int(id_match.group(1)) == frontmatter["id"]
        dated_copies.setdefault(Path(path).name, []).append(text)

    for basename, copies in dated_copies.items():
        assert len(set(copies)) == 1, f"archive copies differ for {basename}"
        created_year = basename[:4]
        created_month = basename[5:7]
        assert any(
            f"/messages/{created_year}/{created_month}/{basename}" in path
            for path in files
        )

    for path, artifact in files.items():
        if "/messages/threads/" not in path:
            continue
        for heading_time, canonical_path in _THREAD_ENTRY_RE.findall(artifact["text"]):
            assert canonical_path in files
            canonical_text = files[canonical_path]["text"]
            frontmatter_text = canonical_text[len("---json\n") :].split(
                "\n---\n", 1
            )[0]
            frontmatter = json.loads(frontmatter_text)
            assert _parse_time(heading_time) == _parse_time(frontmatter["created"])

    if durable["git"]["exists"]:
        changed_paths = {
            line
            for line in durable["git"]["log"].splitlines()
            if line.startswith("projects/")
        }
        assert changed_paths <= set(files)


def _assert_nonempty_record_list(
    by_event: Mapping[str, Mapping[str, Any]],
    event: str,
    required_fields: frozenset[str],
    *,
    count: int,
) -> list[Mapping[str, Any]]:
    records = by_event[event]["result"]["result"]
    assert isinstance(records, list)
    assert len(records) == count
    for record in records:
        assert isinstance(record, Mapping)
        assert required_fields <= record.keys()
    return records


def _assert_relational_ids(tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    project_ids = {row["id"] for row in tables["projects"]}
    agent_ids = {row["id"] for row in tables["agents"]}
    message_ids = {row["id"] for row in tables["messages"]}
    assert all(row["project_id"] in project_ids for row in tables["agents"])
    assert all(row["project_id"] in project_ids for row in tables["messages"])
    assert all(row["sender_id"] in agent_ids for row in tables["messages"])
    assert all(
        row["message_id"] in message_ids and row["agent_id"] in agent_ids
        for row in tables["message_recipients"]
    )
    assert all(
        row["project_id"] in project_ids and row["agent_id"] in agent_ids
        for row in tables["file_reservations"]
    )
    assert all(
        row["a_project_id"] in project_ids
        and row["b_project_id"] in project_ids
        and row["a_agent_id"] in agent_ids
        and row["b_agent_id"] in agent_ids
        for row in tables["agent_links"]
    )


def _assert_raw_integrity(
    output: Mapping[str, Any],
    *,
    namespace: str,
    scenario: str,
) -> None:
    assert output["version"] == 1
    assert output["namespace"] == namespace
    assert output["scenario"] == scenario
    assert tuple(output["tool_trace"]) == _EXPECTED_TOOL_TRACE[scenario]
    assert frozenset(output["tools_used"]) == _scenario_tools(scenario)

    side = "live" if namespace == LIVE_NAMESPACE else "core"
    topology = _expected_topology(side)
    assert output["server"]["tool_count"] == topology["tool_count"]
    assert len(output["server"]["tool_names"]) == topology["tool_count"]
    assert output["server"]["resource_count"] == topology["resource_count"]
    assert output["server"]["resource_template_count"] == topology[
        "resource_template_count"
    ]
    assert output["server"]["prompt_count"] == topology["prompt_count"]
    assert output["server"]["tool_names"] == topology["tool_names"]
    assert output["server"]["resource_names"] == topology["resource_names"]
    assert output["server"]["resource_template_uris"] == topology[
        "resource_template_uris"
    ]
    assert output["server"]["prompt_names"] == topology["prompt_names"]
    if namespace == CORE_NAMESPACE:
        assert frozenset(output["server"]["tool_names"]) == COMPATIBILITY_TOOLS

    checkpoints = output["checkpoints"]
    expected_events = _EXPECTED_EVENTS.get(scenario)
    if expected_events is not None:
        assert tuple(checkpoint["event"] for checkpoint in checkpoints) == expected_events
    assert len({checkpoint["event"] for checkpoint in checkpoints}) == len(checkpoints)

    schema_hashes: set[str] = set()
    for checkpoint in checkpoints:
        started_at = _parse_time(checkpoint["call_window"]["started_at"])
        finished_at = _parse_time(checkpoint["call_window"]["finished_at"])
        assert started_at <= finished_at
        durable = checkpoint["durable"]
        assert durable["database"]["exists"] is True
        assert durable["database"]["integrity_check"] == ["ok"]
        assert durable["database"]["foreign_key_violations"] == []
        assert len(durable["database"]["schema_sha256"]) == 64
        schema_hashes.add(durable["database"]["schema_sha256"])
        tables = durable["database"]["tables"]
        assert set(tables) == _EXPECTED_DATABASE_TABLES
        _assert_relational_ids(tables)
        git = durable["git"]
        if git["exists"]:
            assert git["status_returncode"] == 0
            assert git["status"] == ""
            assert git["fsck_returncode"] == 0
            assert git["log_returncode"] == 0
            commit_count = git["log"].count("--COMMIT--")
            assert git["log"].count(
                "differential-harness <differential-harness@localhost>"
            ) == 2 * commit_count
        for path, artifact in durable["archive"]["files"].items():
            match = _RAW_ARCHIVE_PATH_RE.search(path)
            if match:
                assert (
                    match.group("directory_year"),
                    match.group("directory_month"),
                ) == (
                    match.group("filename_year"),
                    match.group("filename_month"),
                )
                assert "text" in artifact
                created_match = _ARCHIVE_CREATED_RE.search(artifact["text"])
                assert created_match is not None
                filename_time = datetime.strptime(
                    match.group("filename_time"),
                    "%Y-%m-%dT%H-%M-%SZ",
                ).replace(tzinfo=timezone.utc)
                created_time = datetime.fromisoformat(
                    created_match.group(1).replace("Z", "+00:00")
                ).astimezone(timezone.utc)
                assert 0 <= (created_time - filename_time).total_seconds() < 1
        _assert_archive_derivation(durable)
    assert len(schema_hashes) == 1
    assert checkpoints[-1]["durable"]["git"]["log"].count(
        "--COMMIT--"
    ) == _EXPECTED_FINAL_GIT_COMMITS[scenario]

    expected_agents = _EXPECTED_REGISTERED_AGENTS.get(scenario)
    if expected_agents is not None:
        final_database = checkpoints[-1]["durable"]["database"]["tables"]
        markers = {
            row.get("registration_token")
            for row in final_database["agents"]
            if row.get("registration_token") is not None
        }
        assert markers == {
            f"<EXPECTED_TOKEN:{agent_name}>" for agent_name in expected_agents
        }

    if scenario in {"identity", "lifecycle"}:
        _assert_contact_ttl(checkpoints)

    if scenario == "identity":
        by_event = {checkpoint["event"]: checkpoint for checkpoint in checkpoints}
        first_registration = by_event["02_register_green"]["result"]
        replay_registration = by_event["04_reregister_green_same_token"]["result"]
        assert replay_registration["id"] == first_registration["id"]
        assert replay_registration["inception_ts"] == first_registration["inception_ts"]
        assert _parse_time(replay_registration["last_active_ts"]) > _parse_time(
            first_registration["last_active_ts"]
        )

        pending = by_event["07_request_contact"]
        pending_link = pending["durable"]["database"]["tables"]["agent_links"][0]
        assert pending_link["created_ts"] == pending_link["updated_ts"]
        assert (
            _parse_time(pending_link["expires_ts"])
            - _parse_time(pending_link["created_ts"])
        ).total_seconds() == 604800
        assert _parse_time(pending["result"]["expires_ts"]) == _parse_time(
            pending_link["expires_ts"]
        )

        approved = by_event["09_respond_contact_accept"]
        approved_link = approved["durable"]["database"]["tables"]["agent_links"][0]
        assert approved_link["id"] == pending_link["id"]
        assert approved_link["created_ts"] == pending_link["created_ts"]
        assert (
            _parse_time(approved_link["expires_ts"])
            - _parse_time(approved_link["updated_ts"])
        ).total_seconds() == 604800
        assert _parse_time(approved["result"]["expires_ts"]) == _parse_time(
            approved_link["expires_ts"]
        )

        contact_fields = frozenset({"status", "to", "updated_ts", "expires_ts"})
        pending_contacts = _assert_nonempty_record_list(
            by_event,
            "08_list_contacts_pending",
            contact_fields,
            count=1,
        )
        approved_contacts = _assert_nonempty_record_list(
            by_event,
            "10_list_contacts_approved",
            contact_fields,
            count=1,
        )
        assert pending_contacts[0]["status"] == "pending"
        assert approved_contacts[0]["status"] == "approved"

        message_fields = frozenset(
            {
                "id",
                "from",
                "subject",
                "body_md",
                "created_ts",
                "importance",
            }
        )
        blue_inbox = _assert_nonempty_record_list(
            by_event,
            "12_fetch_blue_inbox",
            message_fields | {"kind"},
            count=1,
        )
        topic_messages = _assert_nonempty_record_list(
            by_event,
            "13_fetch_topic",
            message_fields,
            count=1,
        )
        assert blue_inbox[0]["id"] == topic_messages[0]["id"] == 2

        mark_read = by_event["14_mark_message_read"]
        read_recipient = next(
            row
            for row in mark_read["durable"]["database"]["tables"][
                "message_recipients"
            ]
            if row["message_id"] == 2 and row["agent_id"] == 2
        )
        assert _parse_time(mark_read["result"]["read_at"]) == _parse_time(
            read_recipient["read_ts"]
        )

        first_ack = by_event["15_acknowledge_message"]
        ack_recipient = next(
            row
            for row in first_ack["durable"]["database"]["tables"][
                "message_recipients"
            ]
            if row["message_id"] == 2 and row["agent_id"] == 2
        )
        assert _parse_time(first_ack["result"]["read_at"]) == _parse_time(
            ack_recipient["read_ts"]
        )
        assert _parse_time(first_ack["result"]["acknowledged_at"]) == _parse_time(
            ack_recipient["ack_ts"]
        )
        assert _parse_time(ack_recipient["ack_ts"]) >= _parse_time(
            ack_recipient["read_ts"]
        )
        assert by_event["15_acknowledge_message"]["result"] == by_event[
            "16_acknowledge_message_replay"
        ]["result"]
        assert by_event["15_acknowledge_message"]["durable"] == by_event[
            "16_acknowledge_message_replay"
        ]["durable"]

        reply = by_event["17_reply_message"]
        assert reply["result"]["id"] == 3
        assert reply["result"]["reply_to"] == 2
        assert reply["result"]["thread_id"] == "2"
        assert reply["result"]["deliveries"][0]["payload"]["id"] == 3
        reply_row = next(
            row
            for row in reply["durable"]["database"]["tables"]["messages"]
            if row["id"] == 3
        )
        assert _parse_time(reply["result"]["created_ts"]) == _parse_time(
            reply_row["created_ts"]
        )
        green_inbox = _assert_nonempty_record_list(
            by_event,
            "18_fetch_green_inbox",
            message_fields | {"kind", "thread_id"},
            count=1,
        )
        assert green_inbox[0]["id"] == 3
        assert green_inbox[0]["thread_id"] == "2"

    if scenario == "lifecycle":
        by_event = {checkpoint["event"]: checkpoint for checkpoint in checkpoints}
        health = by_event["01_health_checked"]["result"]
        assert health["status"] == "ok"
        assert health["http_host"] == "127.0.0.1"
        assert health["http_port"] == 28317

        session = by_event["05_session_started_with_reservation"]
        assert session["result"]["agent"]["name"] == "GreenCastle"
        assert session["result"]["inbox"] == []
        session_grant = session["result"]["file_reservations"]["granted"][0]
        session_window = session["call_window"]
        session_expiry = _parse_time(session_grant["expires_ts"])
        assert _parse_time(session_window["started_at"]) + timedelta(
            seconds=900
        ) <= session_expiry <= _parse_time(session_window["finished_at"]) + timedelta(
            seconds=900
        )

        cycle = by_event["06_reservation_cycle_auto_released"]
        cycle_grant = cycle["result"]["file_reservations"]["granted"][0]
        cycle_window = cycle["call_window"]
        cycle_expiry = _parse_time(cycle_grant["expires_ts"])
        assert _parse_time(cycle_window["started_at"]) + timedelta(
            seconds=900
        ) <= cycle_expiry <= _parse_time(cycle_window["finished_at"]) + timedelta(
            seconds=900
        )
        assert cycle["result"]["released"]["released"] == 1
        cycle_row = next(
            row
            for row in cycle["durable"]["database"]["tables"][
                "file_reservations"
            ]
            if row["id"] == cycle_grant["id"]
        )
        assert _parse_time(cycle["result"]["released"]["released_at"]) == _parse_time(
            cycle_row["released_ts"]
        )

        handshake = by_event["07_contact_handshake_auto_accepted"]
        assert handshake["result"]["request"] == handshake["result"]["response"]
        assert handshake["result"]["request"]["status"] == "approved"
        assert handshake["result"]["welcome_message"] is None
        link = handshake["durable"]["database"]["tables"]["agent_links"][0]
        assert _parse_time(handshake["result"]["request"]["expires_ts"]) == _parse_time(
            link["expires_ts"]
        )
        assert by_event["08_empty_summary_collection_fetched"]["result"] == {
            "result": []
        }

        retired = by_event["09_peer_retired"]
        assert retired["result"] == {
            "agent_name": "BlueLake",
            "project_key": retired["result"]["project_key"],
            "status": "retired",
        }
        blue = next(
            row
            for row in retired["durable"]["database"]["tables"]["agents"]
            if row["name"] == "BlueLake"
        )
        assert blue["retired_at"] is not None

    if scenario == "reservation_signal":
        by_event = {checkpoint["event"]: checkpoint for checkpoint in checkpoints}

        def signal_agents(event: str) -> list[str]:
            paths = by_event[event]["durable"]["signals"]["files"]
            return sorted(
                path.split("/agents/", 1)[1].split("/", 1)[0]
                for path in paths
            )

        assert signal_agents("13_message_sent_first") == ["BlueLake"]
        assert signal_agents("14_message_sent_second") == ["BlueLake", "BlueLake"]
        assert signal_agents("15_blue_inbox_fetched") == []

        created_rows = by_event["07_reservation_created_nfd"]["durable"][
            "database"
        ]["tables"]["file_reservations"]
        reacquired_rows = by_event["08_reservation_reacquired_nfc_same_agent"][
            "durable"
        ]["database"]["tables"]["file_reservations"]
        assert len(created_rows) == len(reacquired_rows) == 1
        assert created_rows[0]["id"] == reacquired_rows[0]["id"] == 1
        assert created_rows[0]["created_ts"] == reacquired_rows[0]["created_ts"]
        assert reacquired_rows[0]["path_pattern"] == "src/café/**"
        assert reacquired_rows[0]["released_ts"] is None
        initial_expiry = _parse_time(created_rows[0]["expires_ts"])
        initial_window = by_event["07_reservation_created_nfd"]["call_window"]
        assert _parse_time(initial_window["started_at"]) + timedelta(
            seconds=300
        ) <= initial_expiry <= _parse_time(initial_window["finished_at"]) + timedelta(
            seconds=300
        )
        reacquired_expiry = _parse_time(reacquired_rows[0]["expires_ts"])
        reacquired_window = by_event[
            "08_reservation_reacquired_nfc_same_agent"
        ]["call_window"]
        assert _parse_time(reacquired_window["started_at"]) + timedelta(
            seconds=900
        ) <= reacquired_expiry <= _parse_time(
            reacquired_window["finished_at"]
        ) + timedelta(seconds=900)

        conflict = by_event["09_reservation_conflict_other_agent"]["result"]
        assert conflict["granted"] == []
        assert len(conflict["conflicts"]) == 1
        assert conflict["conflicts"][0]["holders"][0]["agent"] == "GreenCastle"
        conflict_rows = by_event["09_reservation_conflict_other_agent"]["durable"][
            "database"
        ]["tables"]["file_reservations"]
        assert conflict_rows == reacquired_rows
        renewal = by_event["10_reservation_renewed_by_overlap"]["result"]
        assert renewal["renewed"] == 1
        renewal_record = renewal["file_reservations"][0]
        old_expiry = datetime.fromisoformat(renewal_record["old_expires_ts"])
        new_expiry = datetime.fromisoformat(renewal_record["new_expires_ts"])
        assert (new_expiry - old_expiry).total_seconds() == 600
        renewed_row = by_event["10_reservation_renewed_by_overlap"]["durable"][
            "database"
        ]["tables"]["file_reservations"][0]
        assert _parse_time(renewal_record["new_expires_ts"]) == _parse_time(
            renewed_row["expires_ts"]
        )
        release = by_event["11_reservation_released_by_overlap"]
        assert release["result"]["released"] == 1
        released_row = release["durable"]["database"]["tables"][
            "file_reservations"
        ][0]
        assert _parse_time(release["result"]["released_at"]) == _parse_time(
            released_row["released_ts"]
        )

        acquired_rows = by_event["12_reservation_acquired_after_release"][
            "durable"
        ]["database"]["tables"]["file_reservations"]
        assert len(acquired_rows) == 2
        second = acquired_rows[1]
        assert second["id"] == 2
        assert acquired_rows[0]["released_ts"] is not None
        assert second["agent_id"] == 2
        assert second["released_ts"] is None
        second_expiry = _parse_time(second["expires_ts"])
        second_window = by_event["12_reservation_acquired_after_release"][
            "call_window"
        ]
        assert _parse_time(second_window["started_at"]) + timedelta(
            seconds=300
        ) <= second_expiry <= _parse_time(second_window["finished_at"]) + timedelta(
            seconds=300
        )
        inbox_records = _assert_nonempty_record_list(
            by_event,
            "15_blue_inbox_fetched",
            frozenset({"id", "from", "subject", "body_md", "kind"}),
            count=2,
        )
        assert [record["id"] for record in inbox_records] == [2, 1]


class _TemporalNormalizer:
    """Normalize absolute time while preserving order and equality classes."""

    def __init__(self, value: Any, state_root: Path) -> None:
        self.state_root = str(state_root)
        self.rich_timing_panel_replacements = 0
        instants = {
            self._instant(match.group(0))
            for text in _walk_strings(value)
            for match in _DATETIME_RE.finditer(text)
        }
        self._instant_ranks = {
            instant: index
            for index, instant in enumerate(sorted(instants), start=1)
        }

    @staticmethod
    def _instant(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _string(self, value: str, *, normalize_rich_timing: bool = False) -> str:
        normalized = value.replace(self.state_root, "<WORKER_STATE>")
        normalized = _ARCHIVE_TIME_RE.sub("<TIME:FILE_Z>", normalized)
        if normalize_rich_timing:
            normalized, replacements = _normalize_rich_timing_presentation(
                normalized
            )
            self.rich_timing_panel_replacements += replacements

        def replace_datetime(match: re.Match[str]) -> str:
            suffix = match.group(2)
            if suffix is None:
                kind = "NAIVE"
            elif suffix in {"Z", "+00:00", "-00:00"}:
                kind = "UTC"
            else:
                kind = "OFFSET"
            rank = self._instant_ranks[self._instant(match.group(0))]
            return f"<TIME:{kind}:{rank:04d}>"

        normalized = _DATETIME_RE.sub(replace_datetime, normalized)
        return _ARCHIVE_DATE_PATH_RE.sub(
            "/<YEAR>/<MONTH>/<TIME:FILE_Z>",
            normalized,
        )

    def normalize(
        self,
        value: Any,
        path: tuple[str | int, ...] = (),
    ) -> Any:
        if isinstance(value, str):
            return self._string(
                value,
                normalize_rich_timing=path[-3:] == _RICH_TIMING_LOG_PATH,
            )
        if isinstance(value, list):
            return [
                self.normalize(item, (*path, index))
                for index, item in enumerate(value)
            ]
        if isinstance(value, Mapping):
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = self._string(str(key))
                if normalized_key in normalized:
                    raise AssertionError(
                        "normalization collapsed distinct mapping keys onto "
                        f"{normalized_key!r}"
                    )
                normalized[normalized_key] = self.normalize(
                    item,
                    (*path, str(key)),
                )
            return normalized
        return value


def _assert_rich_timing_normalization_observed(
    normalizer: _TemporalNormalizer,
    *,
    namespace: str,
    scenario: str,
) -> None:
    assert normalizer.rich_timing_panel_replacements > 0, (
        f"{namespace} {scenario} durable Git log contained no recognized Rich "
        "tool-call timing panel; the differential normalizer may have become "
        "a silent no-op"
    )


def _first_difference(left: Any, right: Any, path: str = "$") -> str | None:
    if type(left) is not type(right):
        return f"{path}: types differ ({type(left).__name__} != {type(right).__name__})"
    if isinstance(left, Mapping):
        if left.keys() != right.keys():
            return f"{path}: mapping keys differ"
        for key in left:
            difference = _first_difference(left[key], right[key], f"{path}/{key}")
            if difference:
                return difference
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path}: list lengths differ ({len(left)} != {len(right)})"
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            difference = _first_difference(
                left_item,
                right_item,
                f"{path}/{index}",
            )
            if difference:
                return difference
        return None
    if left != right:
        return f"{path}: values differ ({left!r} != {right!r})"[:1200]
    return None


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_frozen_live_behavior_matches_core(
    scenario: str,
    frozen_live_checkout: Path,
    tmp_path: Path,
) -> None:
    project_key = tmp_path / "shared-project"
    project_key.mkdir()
    caller_tokens = {
        agent_name: secrets.token_urlsafe(32)
        for agent_name in ("GreenCastle", "BlueLake", "RedStone")
    }
    live_state_root = tmp_path / "live-state"
    core_state_root = tmp_path / "core-state"
    live_roots = WorkerStateRoots.under(
        live_state_root,
        pythonpath=(TESTS_ROOT, frozen_live_checkout / "src"),
    )
    core_roots = WorkerStateRoots.under(
        core_state_root,
        pythonpath=(TESTS_ROOT, CORE_SOURCE),
    )

    live = _run_worker(
        namespace=LIVE_NAMESPACE,
        scenario=scenario,
        roots=live_roots,
        project_key=project_key,
        caller_tokens=caller_tokens,
        source_root=frozen_live_checkout / "src",
    )
    core = _run_worker(
        namespace=CORE_NAMESPACE,
        scenario=scenario,
        roots=core_roots,
        project_key=project_key,
        caller_tokens=caller_tokens,
        source_root=CORE_SOURCE,
    )
    _assert_raw_integrity(live, namespace=LIVE_NAMESPACE, scenario=scenario)
    _assert_raw_integrity(core, namespace=CORE_NAMESPACE, scenario=scenario)

    assert live["tools_used"] == core["tools_used"]
    assert live["tool_trace"] == core["tool_trace"]
    live_normalizer = _TemporalNormalizer(
        live["checkpoints"],
        live_state_root,
    )
    normalized_live = live_normalizer.normalize(live["checkpoints"])
    core_normalizer = _TemporalNormalizer(
        core["checkpoints"],
        core_state_root,
    )
    normalized_core = core_normalizer.normalize(core["checkpoints"])
    if scenario in _RICH_TIMING_PANEL_SCENARIOS:
        _assert_rich_timing_normalization_observed(
            live_normalizer,
            namespace=LIVE_NAMESPACE,
            scenario=scenario,
        )
        _assert_rich_timing_normalization_observed(
            core_normalizer,
            namespace=CORE_NAMESPACE,
            scenario=scenario,
        )
    difference = _first_difference(normalized_live, normalized_core)
    assert difference is None, difference


@pytest.mark.parametrize("icon", ("⚡", "⏱", "🐌"))
@pytest.mark.parametrize("footer", ("⚡ Lightning Fast!", "✓ Fast", "Completed"))
def test_rich_timing_presentation_normalization_is_narrow(
    icon: str,
    footer: str,
    tmp_path: Path,
) -> None:
    rich_panel = (
        "╔════════ ✅ MCP TOOL CALL COMPLETED ════════╗ "
        f"║ ⏱ Duration      │ {icon} 157.97ms      ║ "
        f"╚════════ {footer} ════════╝"
    )
    source = [
        {
            "result": {"body_md": rich_panel},
            "durable": {
                "git": {"log": rich_panel},
                "archive": {
                    "files": {"message.md": {"text": rich_panel}},
                },
                "mapping": {rich_panel: "preserved key"},
            },
        }
    ]

    normalizer = _TemporalNormalizer(source, tmp_path)
    normalized = normalizer.normalize(source)

    assert normalizer.rich_timing_panel_replacements == 1
    assert normalized[0]["durable"]["git"]["log"] == (
        "╔════════ ✅ MCP TOOL CALL COMPLETED ════════╗ "
        "║ ⏱ Duration      │ <DURATION:PRESENTATION> ║ "
        "╚<COMPLETION:PRESENTATION>╝"
    )
    assert normalized[0]["result"]["body_md"] == rich_panel
    assert (
        normalized[0]["durable"]["archive"]["files"]["message.md"]["text"]
        == rich_panel
    )
    assert normalized[0]["durable"]["mapping"] == {rich_panel: "preserved key"}


def test_rich_timing_normalization_rejects_silent_noop(tmp_path: Path) -> None:
    source = [{"durable": {"git": {"log": "plain Git log"}}}]
    normalizer = _TemporalNormalizer(source, tmp_path)

    normalizer.normalize(source)

    with pytest.raises(AssertionError, match="silent no-op"):
        _assert_rich_timing_normalization_observed(
            normalizer,
            namespace=CORE_NAMESPACE,
            scenario="synthetic",
        )


def test_differential_scenarios_cover_exact_compatibility_surface() -> None:
    covered = frozenset().union(*(_scenario_tools(name) for name in _SCENARIOS))
    assert covered == COMPATIBILITY_TOOLS


@pytest.mark.parametrize("namespace", (LIVE_NAMESPACE, CORE_NAMESPACE))
def test_worker_environment_is_fail_closed(
    namespace: str,
    tmp_path: Path,
) -> None:
    hostile = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": "/host/injected",
        "OPENAI_API_KEY": "must-not-cross-worker-boundary",
        "DATABASE_URL": "sqlite:///host-live.sqlite3",
        "STORAGE_ROOT": "/host/live-archive",
        "NOTIFICATIONS_SIGNALS_DIR": "/host/live-signals",
        "HTTP_PORT": "8765",
        "AGENTSTACK_MAIL_DATABASE_URL": "sqlite:///host-core.sqlite3",
        "AGENTSTACK_MAIL_STORAGE_ROOT": "/host/core-archive",
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR": "/host/core-signals",
        "AGENTSTACK_MAIL_HTTP_PORT": "18765",
    }
    roots = WorkerStateRoots.under(
        tmp_path / namespace,
        pythonpath=(TESTS_ROOT, CORE_SOURCE),
    )
    environment = isolated_worker_env(hostile, namespace, roots)

    assert "must-not-cross-worker-boundary" not in environment.values()
    assert environment["PYTHONPATH"] == os.pathsep.join(
        (str(TESTS_ROOT.resolve()), str(CORE_SOURCE.resolve()))
    )
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    for directory in (
        roots.home.parent,
        roots.home,
        roots.storage,
        roots.signals,
        roots.temp,
        roots.cwd,
    ):
        assert directory.stat().st_mode & 0o077 == 0

    if namespace == LIVE_NAMESPACE:
        assert environment["DATABASE_URL"].endswith(str(roots.database))
        assert environment["STORAGE_ROOT"] == str(roots.storage)
        assert environment["NOTIFICATIONS_SIGNALS_DIR"] == str(roots.signals)
        assert environment["HTTP_PORT"] == "28317"
        assert not any(key.startswith("AGENTSTACK_MAIL_") for key in environment)
    else:
        assert environment["AGENTSTACK_MAIL_DATABASE_URL"].endswith(
            str(roots.database)
        )
        assert environment["AGENTSTACK_MAIL_STORAGE_ROOT"] == str(roots.storage)
        assert environment["AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR"] == str(
            roots.signals
        )
        assert environment["AGENTSTACK_MAIL_HTTP_PORT"] == "28317"
        assert "DATABASE_URL" not in environment
        assert "STORAGE_ROOT" not in environment
        assert "NOTIFICATIONS_SIGNALS_DIR" not in environment
        assert "HTTP_PORT" not in environment
