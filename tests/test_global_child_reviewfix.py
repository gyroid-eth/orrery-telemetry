"""Regression inputs from the first child-client review, using real S2c HTTP."""

import pytest
from test_global_child_client import (
    api,
    client,
    reserve,
    shell,
    receipt_count,
    server as fixture_server,
)

server = fixture_server
Error = api["ClientError"]


def test_standalone_root_cannot_be_adopted_as_child(server):
    parent = client(server)
    other = client(server, "other")
    before = {p: p.read_bytes() for p in other.client_root.rglob("*") if p.is_file()}
    count = receipt_count(server)
    with pytest.raises(Error, match="CLIENT_ROOT_NOT_CHILD"):
        parent.prepare_child(
            {"child_client_name": "other", "name": "NewChild", "prepare_only": True}
        )
    assert {
        p: p.read_bytes() for p in other.client_root.rglob("*") if p.is_file()
    } == before
    assert receipt_count(server) == count
    assert not other.outputs["child"].exists()


@pytest.mark.parametrize(
    "hook",
    [
        "check-file-reservation.sh",
        "release-file-reservation.sh",
        "cleanup-child-agent.sh",
        "release-all-reservations.sh",
        "session-start-reminder.sh",
    ],
)
def test_mail_disabled_skips_global_before_owner_material(server, hook):
    c = client(server)
    path, key = reserve(c)
    before = receipt_count(server)
    output = shell(
        c,
        "hooks/" + hook,
        payload={
            "session_id": "one",
            "cwd": str(c.isolation),
            "tool_input": {"file_path": str(c.isolation / "unreserved.py")},
        },
        extra={"AGENTSTACK_MAIL_DISABLED": "1"},
    )
    assert output.returncode == 0, output.stderr
    assert receipt_count(server) == before
    assert c.call("check_file_reservations", {"paths": [path]})["covered"]
    # A disabled hook must not even parse a malformed context.
    broken = c.isolation / "broken-context.json"
    broken.write_text("not JSON")
    output = shell(
        c,
        "hooks/" + hook,
        extra={
            "AGENTSTACK_MAIL_DISABLED": "1",
            "AGENTSTACK_CLIENT_CONFIG": str(broken),
        },
    )
    assert output.returncode == 0, output.stderr


def capture_jobs(monkeypatch):
    jobs = []
    monkeypatch.setattr(
        api["subprocess"], "Popen", lambda args, **kw: jobs.append(args)
    )
    return jobs


def run_job(c, job):
    i = job.index("release-worker")
    return c.release_worker(job[i + 1], [int(k) for k in job[i + 2 :]])


@pytest.mark.parametrize(
    "configured,expected", [(None, 90), ("90", 90), ("7", 7), ("bad", 90)]
)
def test_release_grace_uses_legacy_setting(server, monkeypatch, configured, expected):
    c = client(server)
    path, key = reserve(c)
    jobs = capture_jobs(monkeypatch)
    monkeypatch.delenv("AGENTSTACK_RELEASE_GRACE_SECONDS", raising=False)
    monkeypatch.delenv("FILE_RESERVATION_RELEASE_GRACE_SECONDS", raising=False)
    if configured is not None:
        monkeypatch.setenv("AGENTSTACK_RELEASE_GRACE_SECONDS", configured)
    waits = []
    monkeypatch.setattr(api["time"], "sleep", waits.append)
    c.post_edit({"cwd": str(c.isolation), "tool_input": {"file_path": path}})
    run_job(c, jobs[0])
    assert waits == [expected]
    assert not c.call("check_file_reservations", {"paths": [path]})["covered"]


def test_separate_files_and_empty_post_keep_both_release_plans(server, monkeypatch):
    c = client(server)
    paths = [str(c.isolation / name) for name in ("first.py", "second.py")]
    for path in paths:
        c.call("file_reservation_paths", {"paths": [path]})
    jobs = capture_jobs(monkeypatch)
    monkeypatch.setattr(api["time"], "sleep", lambda _: None)
    for path in paths + [str(c.isolation / "empty.py")]:
        c.post_edit({"cwd": str(c.isolation), "tool_input": {"file_path": path}})
    assert len(jobs) == 2
    for job in jobs:
        assert run_job(c, job)["released"] == 1
    assert not c.call("check_file_reservations", {"paths": paths})["covered"]
    assert list((c.runtime_dir / "release-debounce").iterdir()) == []


@pytest.mark.parametrize("mutation", ["renew", "reacquire"])
def test_worker_keeps_a_reservation_updated_during_grace(server, monkeypatch, mutation):
    c = client(server)
    path, key = reserve(c)
    jobs = capture_jobs(monkeypatch)
    monkeypatch.setattr(api["time"], "sleep", lambda _: None)
    c.post_edit({"cwd": str(c.isolation), "tool_input": {"file_path": path}})
    if mutation == "renew":
        c.call(
            "renew_file_reservations",
            {"file_reservation_ids": [key], "extend_seconds": 1800},
        )
    else:
        # A shorter TTL can preserve expires_ts, so revision must also match.
        assert (
            c.call("file_reservation_paths", {"paths": [path], "ttl_seconds": 60})[
                "granted"
            ][0]["id"]
            == key
        )
    assert run_job(c, jobs[0])["released"] == 0
    assert c.call("check_file_reservations", {"paths": [path]})["covered"]


@pytest.mark.parametrize("tool", ["send_message", "release_file_reservations"])
def test_next_actual_pretool_replays_unrelated_lost_response(server, monkeypatch, tool):
    c = client(server)
    path, key = reserve(c)
    original = c.rpc
    lost = False

    def request(method, params):
        nonlocal lost
        result = original(method, params)
        if params.get("name") == tool and not lost:
            lost = True
            raise Error("TRANSPORT_FAILED")
        return result

    monkeypatch.setattr(c, "rpc", request)
    if tool == "send_message":
        args = {"to_agent_ids": [1], "subject": "note", "body_md": "body"}
    else:
        other = c.call(
            "file_reservation_paths", {"paths": [str(c.isolation / "other.py")]}
        )["granted"][0]["id"]
        args = {"file_reservation_ids": [other]}
    with pytest.raises(Error, match="TRANSPORT_FAILED"):
        c.call(tool, args)
    count = receipt_count(server)
    output = shell(
        c,
        "hooks/check-file-reservation.sh",
        payload={"cwd": str(c.isolation), "tool_input": {"file_path": path}},
    )
    assert output.returncode == 0, output.stderr
    assert receipt_count(server) == count
    assert not c.outputs["mutation"].exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "OtherChild"),
        ("program", "codex"),
        ("model", "other-model"),
        ("task_description", "changed task"),
    ],
)
def test_existing_child_requires_same_parent_and_registration_intent(
    server, field, value
):
    parent = client(server)
    options = {
        "child_client_name": "same_child",
        "name": "SameChild",
        "prepare_only": True,
    }
    first = parent.prepare_child(options)
    child = api["RuntimeClient"](first["client_config"])
    saved = {p: p.read_bytes() for p in child.client_root.rglob("*") if p.is_file()}
    with pytest.raises(Error, match="CHILD_REGISTRATION_INTENT_CONFLICT"):
        parent.prepare_child({**options, field: value})
    assert saved == {
        p: p.read_bytes() for p in child.client_root.rglob("*") if p.is_file()
    }
    assert parent.prepare_child(options)["agent_id"] == first["agent_id"]


def test_zero_grace_releases_synchronously(server, monkeypatch):
    c = client(server)
    path, key = reserve(c)
    monkeypatch.setenv("AGENTSTACK_RELEASE_GRACE_SECONDS", "0")
    monkeypatch.setitem(
        c.post_edit.__globals__["subprocess"].__dict__,
        "Popen",
        lambda *args, **kw: pytest.fail("zero grace must be synchronous"),
    )
    assert (
        c.post_edit({"cwd": str(c.isolation), "tool_input": {"file_path": path}})[
            "released"
        ]
        == 1
    )
    assert not any(row["id"] == key for page in c.all_leases() for row in page)
