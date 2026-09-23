from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import DateTime, text
from sqlalchemy.exc import StatementError
from sqlmodel import SQLModel, Session, create_engine, select

from agentstack_mail.app import _legacy_timestamp_text, _project_to_dict
from agentstack_mail.models import AgentStackUTCDateTime, Project


LEGACY_CREATED = "2026-09-20 01:02:03.123456"


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mail.sqlite3'}")
    SQLModel.metadata.create_all(engine)
    return engine


def test_legacy_naive_sqlite_values_are_read_as_aware_utc(tmp_path) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (slug, human_key, created_at) "
                "VALUES (:slug, :human_key, :created_at)"
            ),
            {
                "slug": "legacy-project",
                "human_key": "/fixture/legacy-project",
                "created_at": LEGACY_CREATED,
            },
        )

    cutoff = datetime(2026, 9, 21, tzinfo=timezone.utc)
    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.created_at < cutoff)
        ).one()
        assert project.created_at == datetime(
            2026, 9, 20, 1, 2, 3, 123456, tzinfo=timezone.utc
        )
        assert _project_to_dict(project)["created_at"] == (
            "2026-09-20T01:02:03.123456+00:00"
        )
        project.archived_at = datetime(2026, 9, 22, 4, 5, 6, tzinfo=timezone.utc)
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.archived_at == datetime(
            2026, 9, 22, 4, 5, 6, tzinfo=timezone.utc
        )
        assert _project_to_dict(project)["archived_at"] == (
            "2026-09-22T04:05:06+00:00"
        )


def test_new_datetime_defaults_and_round_trips_are_aware_utc(tmp_path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        project = Project(slug="aware-project", human_key="/fixture/aware-project")
        assert project.created_at.utcoffset() == timezone.utc.utcoffset(None)
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.created_at.utcoffset() == timezone.utc.utcoffset(None)
        assert _project_to_dict(project)["created_at"].endswith("+00:00")


def test_every_model_datetime_column_uses_the_version_independent_type() -> None:
    timestamp_columns = [
        column
        for table in SQLModel.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, (AgentStackUTCDateTime, DateTime))
    ]
    assert timestamp_columns
    assert all(
        isinstance(column.type, AgentStackUTCDateTime)
        for column in timestamp_columns
    )


def test_existing_timestamp_text_formats_stay_stable() -> None:
    value = datetime(2026, 9, 20, 1, 2, 3, 123456, tzinfo=timezone.utc)
    assert _legacy_timestamp_text(value) == "2026-09-20 01:02:03.123456"
    assert _legacy_timestamp_text(value, separator="T") == (
        "2026-09-20T01:02:03.123456"
    )


def test_new_naive_datetime_writes_are_rejected(tmp_path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(
            Project(
                slug="naive-project",
                human_key="/fixture/naive-project",
                created_at=datetime(2026, 9, 20, 1, 2, 3),
            )
        )
        with pytest.raises(StatementError, match="timezone"):
            session.commit()
