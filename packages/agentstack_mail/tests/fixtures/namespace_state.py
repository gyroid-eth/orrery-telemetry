"""Real legacy SQLite/archive/delivery fixture; never accesses an installed stack."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

from sqlalchemy import create_engine
from sqlmodel import SQLModel, Session
from agentstack_mail import models as m
from agentstack_mail.db import _setup_fts
from agentstack_mail.namespace_state_io import SourceBundle, connection


def build(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    current = datetime(2026, 10, 8, 10, tzinfo=timezone.utc)
    old = current - timedelta(days=400)
    database = root / "mail.sqlite3"
    engine = create_engine("sqlite:///" + str(database))
    SQLModel.metadata.create_all(engine)
    with engine.begin() as sql:
        _setup_fts(sql)
    with Session(engine) as session:
        session.add_all(
            [
                m.Project(id=1, slug="one", human_key="/fixture/one", created_at=old),
                m.Project(id=2, slug="two", human_key="/fixture/two", created_at=old),
            ]
        )
        session.commit()
        session.add_all(
            [
                m.Agent(
                    id=1,
                    project_id=1,
                    name="Alpha",
                    program="fixture",
                    model="fixture",
                    registration_token="fixture-owner-one",
                    credential_generation=7,
                    inception_ts=old,
                    last_active_ts=old,
                    contact_policy="open",
                ),
                m.Agent(
                    id=2,
                    project_id=2,
                    name="Alpha",
                    program="fixture",
                    model="fixture",
                    registration_token="fixture-owner-two",
                    credential_generation=9,
                    inception_ts=old,
                    last_active_ts=old,
                    contact_policy="contacts_only",
                ),
                m.Agent(
                    id=3,
                    project_id=2,
                    name="Gamma",
                    program="fixture",
                    model="fixture",
                    registration_token="fixture-owner-three",
                    credential_generation=2,
                    inception_ts=old,
                    last_active_ts=old,
                ),
                m.Agent(
                    id=4,
                    project_id=1,
                    name="Retired",
                    program="fixture",
                    model="fixture",
                    registration_token=None,
                    credential_generation=0,
                    inception_ts=old,
                    last_active_ts=old,
                    retired_at=old,
                ),
            ]
        )
        session.add(m.MailInstance(instance_id="fixture-instance", created_at=old))
        session.commit()
        for message_id in range(101, 107):
            session.add(
                m.Message(
                    id=message_id,
                    project_id=1 if message_id == 101 else 2,
                    sender_id=1,
                    thread_id=(
                        "101"
                        if message_id == 102
                        else ("review" if message_id == 103 else None)
                    ),
                    topic="fixture-topic",
                    subject="migration fixture " + str(message_id),
                    body_md="検索 fixture message " + str(message_id),
                    importance="high",
                    ack_required=True,
                    created_ts=old if message_id == 101 else current,
                    attachments=(
                        [{"type": "file", "path": "attachments/proof.txt"}]
                        if message_id == 101
                        else []
                    ),
                )
            )
        session.commit()
        for message_id in range(101, 107):
            if message_id != 102:
                session.add(
                    m.MessageRecipient(
                        message_id=message_id,
                        agent_id=2,
                        kind="to",
                        read_ts=old,
                        ack_ts=current,
                    )
                )
        session.add(
            m.MessageRecipient(message_id=102, agent_id=3, kind="bcc", read_ts=current)
        )
        session.add_all(
            [
                m.WindowIdentity(
                    id=11,
                    project_id=1,
                    window_uuid="shared-window",
                    display_name="Alpha",
                    created_ts=old,
                    last_active_ts=current,
                ),
                m.WindowIdentity(
                    id=22,
                    project_id=2,
                    window_uuid="shared-window",
                    display_name="Alpha",
                    created_ts=old,
                    last_active_ts=current,
                ),
                m.FileReservation(
                    id=1,
                    project_id=1,
                    agent_id=1,
                    path_pattern="src/a.py",
                    created_ts=old,
                    expires_ts=current + timedelta(hours=2),
                    exclusive=True,
                ),
                m.FileReservation(
                    id=2,
                    project_id=2,
                    agent_id=2,
                    path_pattern="src/b.py",
                    created_ts=old,
                    expires_ts=current + timedelta(hours=2),
                    exclusive=True,
                ),
                m.FileReservation(
                    id=3,
                    project_id=2,
                    agent_id=3,
                    path_pattern="unknown-history.py",
                    created_ts=old,
                    expires_ts=old,
                    exclusive=True,
                ),
                m.AgentLink(
                    id=1,
                    a_project_id=1,
                    a_agent_id=1,
                    b_project_id=2,
                    b_agent_id=2,
                    status="approved",
                    created_ts=old,
                    updated_ts=current,
                ),
                m.Product(
                    id=1, product_uid="product-fixture", name="fixture", created_at=old
                ),
                m.MessageSummary(
                    id=1,
                    project_id=1,
                    summary_text="legacy summary",
                    start_ts=old,
                    end_ts=current,
                    source_message_count=2,
                    source_thread_ids='["101","review"]',
                    created_ts=current,
                ),
                m.ProjectSiblingSuggestion(
                    id=1,
                    project_a_id=1,
                    project_b_id=2,
                    score=0.5,
                    rationale="fixture",
                    created_ts=old,
                ),
            ]
        )
        session.commit()
        session.add(
            m.ProductProjectLink(id=1, product_id=1, project_id=1, created_at=old)
        )
        session.add(
            m.EnrollmentAudit(
                id=1,
                occurred_at=old,
                request_id="audit-fixture",
                peer_uid=501,
                server_instance_id="fixture-instance",
                project_key="/fixture/two",
                agent_id=2,
                operation="recover",
                old_generation=8,
                new_generation=9,
                new_fingerprint="f" * 64,
                reason="fixture",
                result="ok",
            )
        )
        session.commit()
    engine.dispose()
    delivery = root / "delivery.sqlite3"
    sql = (
        Path(__file__).resolve().parents[2]
        / "fixtures/namespace-legacy-delivery-v1.sql"
    )
    with connection(delivery, write=True) as target:
        target.executescript(sql.read_text())
        for message_id, status in zip(
            (101, 106, 103, 104, 105),
            ("delivered", "dead_letter", "pending", "failed", "leased"),
        ):
            target.execute(
                "INSERT INTO codex_app_delivery_state(project_key,agent_name,message_id,status,lease_owner,lease_expires_at,attempt_count,last_error,created_at,updated_at,delivered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "/fixture/two",
                    "Alpha",
                    message_id,
                    status,
                    "fixture-wake" if status == "leased" else None,
                    (
                        (current + timedelta(minutes=2)).isoformat()
                        if status == "leased"
                        else None
                    ),
                    3,
                    "fixture failure" if status in ("failed", "dead_letter") else None,
                    old.isoformat(),
                    current.isoformat(),
                    current.isoformat() if status == "delivered" else None,
                ),
            )
    for name in ("archive", "signals", "history", "workspace"):
        (root / name).mkdir()
    archive = root / "archive/projects/one/attachments"
    archive.mkdir(parents=True)
    (archive / "proof.txt").write_bytes(b"attachment fixture\x00bytes")
    profile = root / "archive/projects/one/agents/Alpha"
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text(
        json.dumps(
            {"id": 1, "name": "Alpha", "registration_token": "fixture-owner-one"}
        )
    )
    signal = root / "signals/projects/two/agents/Alpha"
    signal.mkdir(parents=True)
    (signal / "103.signal").write_text(
        json.dumps(
            {
                "project": "two",
                "agent": "Alpha",
                "timestamp": current.isoformat(),
                "message": {"id": 103, "from": "Alpha", "subject": "fixture"},
            }
        )
    )
    (root / "history/audit.log").write_bytes(b"legacy history remains\n")
    bindings = {
        "version": 1,
        "bindings": [
            {
                "session_id": "session-" + str(agent),
                "agent_id": agent,
                "agent_name": "Alpha",
                "mail_instance_id": "fixture-instance",
                "project_key": "/fixture/" + project,
                "registration_token": token,
                "credential_generation": generation,
                "window_row_id": window,
                "window_uuid": "shared-window",
            }
            for agent, project, token, generation, window in [
                (1, "one", "fixture-owner-one", 7, 11),
                (2, "two", "fixture-owner-two", 9, 22),
            ]
        ],
    }
    (root / "bindings.json").write_text(json.dumps(bindings))
    config = {
        "expected_writers": [
            "mail",
            "delivery",
            "archive",
            "signals",
            "lease-gc",
            "retention",
        ],
        "instruction_blocks": [],
        "delivery_policies": {
            json.dumps(["/fixture/two", "Alpha"], separators=(",", ":")): {
                "version": "retry-v1",
                "coalesce_seconds": 1,
                "base_backoff_seconds": 2,
                "max_backoff_seconds": 60,
            }
        },
    }
    (root / "config.json").write_text(json.dumps(config))
    choices = {
        "rename_map": {"2": "Beta"},
        "window_map": {"11": "keep", "22": "generate"},
        "lease_anchors": {"1": str(root / "workspace"), "2": str(root / "workspace")},
        "wake_recovery": [
            json.dumps(["/fixture/two", "Alpha", 105], separators=(",", ":"))
        ],
        "as_of": current.isoformat(),
    }
    return (
        SourceBundle(
            database,
            delivery,
            root / "archive",
            root / "signals",
            root / "history",
            root / "bindings.json",
            root / "config.json",
        ),
        choices,
    )
