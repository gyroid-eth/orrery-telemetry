"""What the guards do when nobody can register, and when nobody claims a session.

A tester reported that an IDE agent panel was refused on its first Edit, with a
message naming a recovery it could not perform: the registration flag has one
writer, the PostToolUse hook on the mail MCP's register_agent, and
`agentstack-reregister` does not write it. The first reading was that the client
was structurally excluded. It was not -- the same client had registered fine on
other days, and what was broken that day was the mail service.

So two questions are asked separately, and this file pins both:

  transport  is the service answering? While it is not, no session can register
             and no reservation can be taken or checked by anyone, so a refusal
             protects nothing that is still alive.
  identity   does this session have an identity source? While the service is
             answering, a session without one can register, so it is asked to.

Conflating them made the guards decide by launch path: a healthy server still
waved raw clients through, and an outage still trapped tmux ones.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRATION_GUARD = REPO_ROOT / "hooks" / "check-agent-registered.sh"
RESERVATION_GUARD = REPO_ROOT / "hooks" / "check-file-reservation.sh"

BASE_ENV = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
}


def _run(guard: Path, payload: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    # Every guard decision starts with "is the mail service answering", so a
    # test that does not say which service it means is really testing whatever
    # is running on the developer's machine. Nine of these went green and then
    # red an hour later for exactly that reason, when the local service stopped.
    assert "AGENTSTACK_MCP_URL" in env, (
        "say which endpoint this test means: UNREACHABLE, or the answering_endpoint fixture"
    )
    environment = dict(BASE_ENV)
    environment["HOME"] = str(tmp_path / "home")
    environment["TMPDIR"] = str(tmp_path / "tmp")
    environment["AGENTSTACK_RUNTIME_DIR"] = str(tmp_path / "runtime")
    environment["AGENTSTACK_HOOKS_DIR"] = str(REPO_ROOT / "hooks")
    Path(environment["HOME"]).mkdir(parents=True, exist_ok=True)
    Path(environment["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    environment.update(env)
    return subprocess.run(
        ["/bin/bash", str(guard)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )


def _registration_payload(session_id: str) -> str:
    return (
        '{"session_id": "%s", "hook_event_name": "PreToolUse", '
        '"tool_name": "Bash", "tool_input": {"command": "ls"}}' % session_id
    )


def _edit_payload(session_id: str, file_path: Path) -> str:
    return (
        '{"session_id": "%s", "hook_event_name": "PreToolUse", '
        '"tool_name": "Edit", "tool_input": {"file_path": "%s"}}' % (session_id, file_path)
    )


# --- the service is what decides whether registering is possible ----------


UNREACHABLE = "http://127.0.0.1:1/api/"


@pytest.fixture()
def answering_endpoint():
    """Something listening. What it answers does not matter.

    "The server said no" is not "the server is gone": an endpoint that rejects,
    errors, or is not even ours still means a session could be talking to
    something, and the guards stay closed.
    """
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib naming
            self.send_response(500)
            self.end_headers()

        def do_GET(self):  # noqa: N802 - stdlib naming
            self.send_response(500)
            self.end_headers()

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}/api/"
    finally:
        server.shutdown()
        server.server_close()


def test_an_unregistered_session_is_refused_while_the_service_answers(
    tmp_path: Path, answering_endpoint: str
) -> None:
    """The default: registering is possible, so it is required."""
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("healthy-1"),
        tmp_path,
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "UNMANAGED SESSION BLOCKED" in result.stderr
    assert "register_agent" in result.stderr


def test_an_endpoint_that_only_errors_is_still_a_reachable_service(
    tmp_path: Path, answering_endpoint: str
) -> None:
    """An outage escape must not be opened by a server that answers badly."""
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.md"
    target.write_text("x", encoding="utf-8")
    result = _run(
        RESERVATION_GUARD,
        _edit_payload("healthy-2", target),
        tmp_path,
        AGENTSTACK_PROTECTED_ROOTS=str(project),
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "UNREACHABLE" not in result.stderr


def test_nobody_is_refused_while_the_service_is_down(tmp_path: Path) -> None:
    """The reported defect.

    Registration is impossible during an outage, for every client and every
    launch path, so a refusal here demands something nobody can do.
    """
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("outage-1"),
        tmp_path,
        AGENTSTACK_MCP_URL=UNREACHABLE,
    )
    assert result.returncode == 0, result.stderr
    assert "systemMessage" in result.stdout, "the outage has to be visible"


def test_the_edit_guard_agrees_during_an_outage(tmp_path: Path) -> None:
    """Letting it past one guard and trapping it at the other is not a fix."""
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.md"
    target.write_text("x", encoding="utf-8")
    result = _run(
        RESERVATION_GUARD,
        _edit_payload("outage-2", target),
        tmp_path,
        AGENTSTACK_PROTECTED_ROOTS=str(project),
        AGENTSTACK_PROJECT_KEY=str(project),
        AGENTSTACK_MCP_URL=UNREACHABLE,
    )
    assert result.returncode == 0, result.stderr


def test_an_outage_does_not_register_anything(tmp_path: Path) -> None:
    """Degrading is not registering.

    If the outage left a flag behind, the session would stay "registered" after
    the service came back, and would never be asked for the identity it never
    established.
    """
    session_id = "outage-3"
    flag = Path("/tmp") / f".claude-agent-registered-{session_id}"
    flag.unlink(missing_ok=True)
    result = _run(
        REGISTRATION_GUARD, _registration_payload(session_id), tmp_path,
        AGENTSTACK_MCP_URL=UNREACHABLE,
    )
    assert result.returncode == 0
    assert not flag.exists()
    index = tmp_path / "runtime" / "session_index"
    assert not index.exists() or not list(index.glob("*.json"))


def test_the_refusal_returns_when_the_service_does(
    tmp_path: Path, answering_endpoint: str
) -> None:
    """Recovery is re-evaluated per call, not remembered as permission."""
    session_id = "recovery-1"
    during = _run(REGISTRATION_GUARD, _registration_payload(session_id), tmp_path,
                  AGENTSTACK_MCP_URL=UNREACHABLE)
    assert during.returncode == 0
    after = _run(REGISTRATION_GUARD, _registration_payload(session_id), tmp_path,
                 AGENTSTACK_MCP_URL=answering_endpoint)
    assert after.returncode == 2, "the outage was remembered as permission"


def _assert_recovery_lines_are_runnable(stderr: str, tmp_path: Path) -> None:
    """Every command offered must exist on this machine.

    The first version named `agentstack-mailctl start` unconditionally. That
    file is not installed everywhere -- it was absent on the machine writing
    this -- so the refusal pointed at a recovery the reader could not run,
    which is the shape of the defect this whole change is about. Pinning the
    string in a test only protected the wrong advice.
    """
    offered = [line for line in stderr.splitlines() if line.startswith("Run: ")]
    for line in offered:
        command = line[len("Run: ") :].split()[0]
        assert Path(command).exists() and os.access(command, os.X_OK), (
            f"offered a command that is not executable here: {command}"
        )
    if not offered:
        assert "No agentstack tools were found" in stderr, stderr


def test_an_installation_may_choose_to_stop_instead(tmp_path: Path) -> None:
    """Some operators would rather halt than write uncoordinated."""
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("outage-4"),
        tmp_path,
        AGENTSTACK_MCP_URL=UNREACHABLE,
        AGENTSTACK_MAIL_OUTAGE_POLICY="block",
    )
    assert result.returncode == 2
    assert "AGENT MAIL UNREACHABLE" in result.stderr
    _assert_recovery_lines_are_runnable(result.stderr, tmp_path)


def test_a_conflicting_identity_is_refused_even_during_an_outage(tmp_path: Path) -> None:
    """An outage is not an escape from an ambiguous identity."""
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.md"
    target.write_text("x", encoding="utf-8")
    _bind(tmp_path, 41, "outage-conflict", "FirstName", str(project))
    _bind(tmp_path, 77, "outage-conflict", "SecondName", str(project))
    result = _run(
        RESERVATION_GUARD,
        _edit_payload("outage-conflict", target),
        tmp_path,
        AGENTSTACK_PROTECTED_ROOTS=str(project),
        AGENTSTACK_PROJECT_KEY=str(project),
        AGENTSTACK_MCP_URL=UNREACHABLE,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "AGENT IDENTITY CONFLICT" in result.stderr


def test_an_operator_may_run_a_client_outside_coordination(
    tmp_path: Path, answering_endpoint: str
) -> None:
    """The opt-out still exists, but it has to be chosen."""
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("optout-1"),
        tmp_path,
        AGENTSTACK_MCP_URL=answering_endpoint,
        AGENTSTACK_UNMANAGED_SESSION_POLICY="warn-open",
    )
    assert result.returncode == 0, result.stderr
    assert "systemMessage" in result.stdout


# --- the null case: the guard must still be able to say no ---------------


def test_a_tmux_session_without_registration_is_still_refused(tmp_path: Path, answering_endpoint: str) -> None:
    """The normal terminal path is untouched: identity is reachable there.

    Without this, "allow the unmanaged ones" would be indistinguishable from
    "allow everyone", and the guard would be decorative.
    """
    bin_dir = _fake_tmux(tmp_path, "RealSession")
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("tmux-1"),
        tmp_path,
        TMUX="/tmp/tmux-501/default,1,0",
        TMUX_PANE="%3",
        PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "AGENT NOT REGISTERED" in result.stderr


def test_a_registered_session_is_allowed(tmp_path: Path, answering_endpoint: str) -> None:
    session_id = "tmux-2"
    flag = Path("/tmp") / f".claude-agent-registered-{session_id}"
    flag.write_text("", encoding="utf-8")
    try:
        result = _run(
            REGISTRATION_GUARD,
            _registration_payload(session_id),
            tmp_path,
            TMUX="/tmp/tmux-501/default,1,0",
            AGENTSTACK_MCP_URL=answering_endpoint,
        )
        assert result.returncode == 0, result.stderr
    finally:
        flag.unlink(missing_ok=True)


def test_an_agent_name_still_passes_immediately(tmp_path: Path, answering_endpoint: str) -> None:
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("named-1"),
        tmp_path,
        AGENT_NAME="IcyGauss",
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 0, result.stderr


# --- the policy switch ---------------------------------------------------


def test_block_policy_refuses_and_names_an_escape_from_outside(tmp_path: Path, answering_endpoint: str) -> None:
    """An operator may prefer to lose the client rather than the coordination.

    The refusal then has to name a way out that does not run inside the session,
    because everything inside it is what was just refused.
    """
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("zed-3"),
        tmp_path,
        AGENTSTACK_UNMANAGED_SESSION_POLICY="block",
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 2
    assert "UNMANAGED SESSION BLOCKED" in result.stderr
    assert "agent-start" in result.stderr
    assert "settings.json" in result.stderr


def test_block_policy_also_refuses_the_edit(tmp_path: Path, answering_endpoint: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.md"
    target.write_text("x", encoding="utf-8")
    result = _run(
        RESERVATION_GUARD,
        _edit_payload("zed-4", target),
        tmp_path,
        AGENTSTACK_PROTECTED_ROOTS=str(project),
        AGENTSTACK_UNMANAGED_SESSION_POLICY="block",
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 2
    assert "UNMANAGED SESSION BLOCKED" in result.stderr


@pytest.mark.parametrize("value", ["", "open", "Warn-Open", "yes", "0"])
def test_an_unreadable_policy_value_fails_closed(tmp_path: Path, value: str, answering_endpoint: str) -> None:
    """A typo in the setting must not be the loose side of the choice.

    An empty value counts as a typo, not as "unset": somebody wrote the setting
    and got it wrong, and that is exactly when a guard should not relax.
    """
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload(f"typo-{value or 'empty'}"),
        tmp_path,
        AGENTSTACK_UNMANAGED_SESSION_POLICY=value,
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 2, f"{value!r} was treated as permission"


def test_leaving_the_policy_unset_takes_the_default(tmp_path: Path) -> None:
    """The null case for the test above: no opinion is not a typo.

    The default for a session with no identity is to require registration, so
    "unset" and "typo" now agree on the outcome -- what distinguishes them is
    the outage policy, which unset leaves at warn-open (see the outage tests).
    """
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("unset-1"),
        tmp_path,
        AGENTSTACK_MCP_URL=UNREACHABLE,
    )
    assert result.returncode == 0, "an unset outage policy stopped work during an outage"


# --- the degrade has to leave a trace ------------------------------------


def test_the_degrade_is_recorded(tmp_path: Path) -> None:
    """stderr on an allowed call reaches nobody; the log is the durable half."""
    _run(REGISTRATION_GUARD, _registration_payload("zed-5"), tmp_path,
         AGENTSTACK_MCP_URL=UNREACHABLE)
    log = tmp_path / "runtime" / "logs" / "unmanaged_sessions.jsonl"
    assert log.exists(), "no audit trail for a guard that stood down"
    body = log.read_text(encoding="utf-8")
    assert "zed-5" in body
    assert "check-agent-registered" in body


def test_the_warning_is_not_repeated_for_every_tool_call(tmp_path: Path) -> None:
    first = _run(REGISTRATION_GUARD, _registration_payload("zed-6"), tmp_path,
                 AGENTSTACK_MCP_URL=UNREACHABLE)
    second = _run(REGISTRATION_GUARD, _registration_payload("zed-6"), tmp_path,
                  AGENTSTACK_MCP_URL=UNREACHABLE)
    assert "systemMessage" in first.stdout, "the warning has to be visible somewhere"
    assert "systemMessage" not in second.stdout
    log = (tmp_path / "runtime" / "logs" / "unmanaged_sessions.jsonl").read_text(encoding="utf-8")
    assert log.count("zed-6") == 2, "the audit log should still record both"


# --- a session that did register is not anonymous ------------------------


RECORDER = REPO_ROOT / "hooks" / "record-session-index.py"


def _record_registration(
    tmp_path: Path,
    session_id: str,
    agent_id: int,
    agent_name: str,
    project_key: str,
    caller: str = "",
    caller_source: str = "tmux-session",
) -> int:
    """Drive the real PostToolUse writer, not a hand-made fixture.

    The first version of this feature ordered index entries by a `ts` field the
    reader parsed as a number while the writer emitted an ISO string. Every
    hand-written fixture used numbers, so the suite was green and the feature
    was broken. Fixtures describe what the reader accepts; only the writer
    describes what it will actually be handed.
    """
    payload = json.dumps(
        {
            "session_id": session_id,
            "transcript_path": str(tmp_path / f"{session_id}.jsonl"),
            "cwd": project_key,
            "tool_input": {"name": agent_name, "project_key": project_key},
            "tool_response": {"id": agent_id, "name": agent_name},
        }
    )
    environment = dict(BASE_ENV)
    environment["HOME"] = str(tmp_path / "home")
    environment["AGENTSTACK_RUNTIME_DIR"] = str(tmp_path / "runtime")
    if caller:
        # What mark-agent-registered.sh passes once it has resolved the caller.
        environment["AGENTSTACK_REGISTERING_AGENT"] = caller
        environment["AGENTSTACK_REGISTERING_SOURCE"] = caller_source
    result = subprocess.run(
        ["python3", str(RECORDER)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    return result.returncode


def _write_raw_index_record(tmp_path: Path, agent_id: int, record: dict) -> None:
    """Write a record the production writer would never produce.

    Only for the schema tests below. Everything else drives the real writer --
    a hand-made fixture describes what the reader accepts, not what it will be
    handed, and that gap is what hid the first version's schema mismatch.
    """
    index = tmp_path / "runtime" / "session_index"
    index.mkdir(parents=True, exist_ok=True)
    (index / f"{agent_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _resolve(tmp_path: Path, session_id: str, **env: str) -> str:
    """Ask resolve-agent-name.sh who this session is."""
    # The id travels in the environment, never inside the shell string: a
    # hostile value has to reach the code under test, not break the harness.
    script = (
        f'. {REPO_ROOT / "hooks" / "resolve-agent-name.sh"}; '
        'printf "%s|%s" "$RESOLVED_AGENT" "$RESOLVED_AGENT_SRC"'
    )
    environment = dict(BASE_ENV)
    environment["HOME"] = str(tmp_path / "home")
    environment["AGENTSTACK_RUNTIME_DIR"] = str(tmp_path / "runtime")
    environment["AGENTSTACK_SESSION_ID"] = session_id
    environment.update(env)
    return subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    ).stdout


def test_a_registered_session_without_tmux_is_identified(tmp_path: Path) -> None:
    """Registering is what grants identity -- not being launched from tmux.

    Without this the desktop app and IDE panels stay anonymous even after they
    call register_agent, and the reservation guard has no name to check.
    """
    _record_registration(tmp_path, "acp-1", 41, "IcyGauss", str(tmp_path / "project"))
    assert _resolve(tmp_path, "acp-1") == "IcyGauss|session-index"


def test_an_unknown_session_stays_unresolved(tmp_path: Path) -> None:
    """The null case: the index must not invent a name for a stranger."""
    _record_registration(tmp_path, "acp-1", 41, "IcyGauss", str(tmp_path / "project"))
    assert _resolve(tmp_path, "acp-other") == "|none"


def _bind(tmp_path: Path, agent_id: int, session_id: str, name: str, project: str) -> None:
    """A binding that already exists, however it got there.

    The writer refuses to create a second one (see the test below), but two can
    still exist from concurrent writes or from a machine that ran an older
    version, and the readers have to cope with what is on disk.
    """
    _write_raw_index_record(
        tmp_path,
        agent_id,
        {
            "agent_id": agent_id,
            "agent_name": name,
            "session_id": session_id,
            "project_key": project,
            "registered_by": "",
            "schema_version": 2,
            "binding_kind": "self",
            "ts": "2026-08-22T00:00:00",
        },
    )


def test_two_identities_for_one_session_is_a_conflict(tmp_path: Path) -> None:
    """Do not pick a winner by timestamp.

    Ordering by time reads a rename into what may be a mix-up, and it depends on
    a field whose format the writer is free to change. Refusing is the answer
    that cannot attribute a write to an agent that did not make it.
    """
    project = str(tmp_path / "project")
    _bind(tmp_path, 41, "acp-2", "OldName", project)
    _bind(tmp_path, 77, "acp-2", "NewName", project)
    assert _resolve(tmp_path, "acp-2") == "|identity-conflict"


@pytest.mark.parametrize("hostile", ["../../etc/passwd", "a/b", "x;y", "a b", "'"])
def test_a_session_id_that_is_not_one_is_refused(tmp_path: Path, hostile: str) -> None:
    """The id reaches the filesystem, so it is checked before it is used."""
    _record_registration(tmp_path, hostile, 41, "Sneaky", str(tmp_path / "project"))
    assert _resolve(tmp_path, hostile) == "|none"


def test_the_environment_still_wins_over_the_index(tmp_path: Path) -> None:
    _record_registration(tmp_path, "acp-3", 41, "IndexName", str(tmp_path / "project"))
    assert _resolve(tmp_path, "acp-3", AGENT_NAME="EnvName") == "EnvName|env"


def test_an_identified_session_follows_the_outage_policy_too(
    tmp_path: Path,
) -> None:
    """An identified agent is not more entitled to write uncoordinated.

    The outage branch used to sit only on the path for sessions with no name,
    so an operator who chose `block` still had every named agent editing
    through the outage -- the guards disagreeing about the same condition.
    """
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.md"
    target.write_text("x", encoding="utf-8")
    _record_registration(tmp_path, "acp-4", 41, "IcyGauss", str(project))
    common = dict(
        AGENTSTACK_PROTECTED_ROOTS=str(project),
        AGENTSTACK_PROJECT_KEY=str(project),
        AGENTSTACK_MCP_URL=UNREACHABLE,
        AGENTSTACK_MAIL_HTTP_BEARER_MODE="disabled",
    )
    refused = _run(
        RESERVATION_GUARD,
        _edit_payload("acp-4", target),
        tmp_path,
        AGENTSTACK_MAIL_OUTAGE_POLICY="block",
        **common,
    )
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "AGENT MAIL UNREACHABLE" in refused.stderr

    allowed = _run(
        RESERVATION_GUARD,
        _edit_payload("acp-4", target),
        tmp_path,
        AGENTSTACK_MAIL_OUTAGE_POLICY="warn-open",
        **common,
    )
    assert allowed.returncode == 0, allowed.stderr
    log = tmp_path / "runtime" / "logs" / "unmanaged_sessions.jsonl"
    assert log.exists(), "the degrade for a named agent was not recorded"
    assert "IcyGauss" in log.read_text(encoding="utf-8")


# --- the writer and the reader have to agree ----------------------------

def test_what_the_writer_writes_is_what_the_reader_reads(tmp_path: Path) -> None:
    project = str(tmp_path / "project")
    _record_registration(tmp_path, "prod-1", 41, "IcyGauss", project)
    assert (
        _resolve(tmp_path, "prod-1", AGENTSTACK_LOOKUP_PROJECT_KEY=project)
        == "IcyGauss|session-index"
    )


def test_an_anonymous_caller_cannot_claim_a_session_that_is_already_bound(
    tmp_path: Path,
) -> None:
    """Refusing conflicts at the reader is late; better not to create them.

    A caller with no resolvable identity may bind a session nobody has claimed.
    Once somebody has, a second anonymous claim is a stranger's, not a rename.
    """
    project = str(tmp_path / "project")
    assert _record_registration(tmp_path, "prod-2", 41, "FirstName", project) == 0
    assert _record_registration(tmp_path, "prod-2", 77, "SecondName", project) == 5
    assert (
        _resolve(tmp_path, "prod-2", AGENTSTACK_LOOKUP_PROJECT_KEY=project)
        == "FirstName|session-index"
    )


def test_registering_a_child_does_not_rename_the_parent_session(tmp_path: Path) -> None:
    """A session that registers somebody else has not become them."""
    project = str(tmp_path / "project")
    _record_registration(tmp_path, "parent-1", 41, "ChildAgent", project, caller="ParentAgent")
    assert _resolve(tmp_path, "parent-1", AGENTSTACK_LOOKUP_PROJECT_KEY=project) == "|none"


def test_a_binding_from_another_project_is_not_authority_here(tmp_path: Path) -> None:
    """Agent names are project-local, so a binding does not travel."""
    project_a = str(tmp_path / "a")
    project_b = str(tmp_path / "b")
    _record_registration(tmp_path, "cross-1", 41, "CrossName", project_a)
    assert _resolve(tmp_path, "cross-1", AGENTSTACK_LOOKUP_PROJECT_KEY=project_a) == "CrossName|session-index"
    assert _resolve(tmp_path, "cross-1", AGENTSTACK_LOOKUP_PROJECT_KEY=project_b) == "|none"


def test_a_registered_session_with_no_binding_is_refused_not_waved_through(
    tmp_path: Path, answering_endpoint: str,) -> None:
    """The gap between "registered" and "identifiable" must not read as unmanaged.

    The flag proves this session reached the mail MCP, so it can take a
    reservation like anyone else. Treating it as a client that cannot
    coordinate would skip the check for an agent that simply has not been
    written down yet.
    """
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.md"
    target.write_text("x", encoding="utf-8")
    session_id = "registered-no-binding"
    flag = Path("/tmp") / f".claude-agent-registered-{session_id}"
    flag.write_text("", encoding="utf-8")
    try:
        result = _run(
            RESERVATION_GUARD,
            _edit_payload(session_id, target),
            tmp_path,
            AGENTSTACK_PROTECTED_ROOTS=str(project),
            AGENTSTACK_PROJECT_KEY=str(project),
            AGENTSTACK_MCP_URL=answering_endpoint,
        )
        assert result.returncode == 2, result.stdout + result.stderr
        assert "AGENT IDENTITY UNRESOLVED" in result.stderr
    finally:
        flag.unlink(missing_ok=True)


def test_the_index_is_written_before_the_flag(tmp_path: Path) -> None:
    """Ordering, read from the commands rather than from the prose.

    While the flag exists and the binding does not, a guard reading the flag
    believes the session is registered and a guard reading the index cannot say
    who it is -- the window the test above refuses. An earlier version of this
    test matched the word anywhere in the file, so a comment mentioning the
    writer satisfied it while the call itself sat below the flag.
    """
    body = (REPO_ROOT / "hooks" / "mark-agent-registered.sh").read_text(encoding="utf-8")
    commands = [
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    ]
    index_write = next(
        i for i, line in enumerate(commands) if "record-session-index.py" in line and "|" in line
    )
    flag_write = next(i for i, line in enumerate(commands) if 'touch "$FLAG"' in line)
    assert index_write < flag_write, "the flag is set before the identity is recorded"
    assert not any(
        "record-session-index.py" in line and line.rstrip().endswith("&") for line in commands
    ), "the index write is backgrounded again, so the flag can outrun it"


# --- records that are not bindings ---------------------------------------


@pytest.mark.parametrize(
    ("label", "record"),
    [
        ("written by an older version", {"agent_name": "OldSchema", "session_id": "schema-1"}),
        (
            "a kind that is not a self binding",
            {
                "agent_name": "Delegated",
                "session_id": "schema-1",
                "schema_version": 2,
                "binding_kind": "delegated",
                "registered_by": "Parent",
            },
        ),
        (
            "a caller field of the wrong type",
            {
                "agent_name": "MalformedCaller",
                "session_id": "schema-1",
                "schema_version": 2,
                "binding_kind": "self",
                "registered_by": {"name": "Parent"},
            },
        ),
    ],
)
def test_a_record_that_cannot_prove_it_is_a_binding_is_ignored(
    tmp_path: Path, label: str, record: dict
) -> None:
    """Accepting the ambiguous ones is how a wrong record gains authority."""
    _write_raw_index_record(tmp_path, 41, record)
    assert _resolve(tmp_path, "schema-1") == "|none", label


def test_the_writer_does_not_record_a_registration_made_for_someone_else(
    tmp_path: Path,
) -> None:
    """Filtering at the reader is not enough: other readers exist.

    The dashboard reads this index too, and used a parent's record to show the
    parent's transcript on the child's card. A record nobody should trust is
    better not written.
    """
    project = str(tmp_path / "project")
    _record_registration(tmp_path, "parent-2", 41, "ChildAgent", project, caller="ParentAgent")
    index = tmp_path / "runtime" / "session_index"
    assert not list(index.glob("*.json")), "a delegated registration was recorded anyway"


# --- guard-level, not resolver-level -------------------------------------


def test_a_conflicting_session_is_refused_by_the_guard_itself(tmp_path: Path, answering_endpoint: str) -> None:
    """The refusal has to survive the trip back into the guard.

    The resolver said "conflict" and the guard read only the name, so an empty
    name arrived and the session looked anonymous -- which the policy then
    waved through. Resolver-level tests cannot see that.
    """
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.md"
    target.write_text("x", encoding="utf-8")
    _bind(tmp_path, 41, "conflict-guard", "FirstName", str(project))
    _bind(tmp_path, 77, "conflict-guard", "SecondName", str(project))
    result = _run(
        RESERVATION_GUARD,
        _edit_payload("conflict-guard", target),
        tmp_path,
        AGENTSTACK_PROTECTED_ROOTS=str(project),
        AGENTSTACK_PROJECT_KEY=str(project),
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "AGENT IDENTITY CONFLICT" in result.stderr
    log = tmp_path / "runtime" / "logs" / "unmanaged_sessions.jsonl"
    assert not log.exists(), "a conflict was recorded as an unmanaged session"


def test_the_audit_line_cannot_be_forged_through_a_session_id(tmp_path: Path) -> None:
    """The id and the path come from tool input, so they are data, not format."""
    hostile = 'x", "event": "nothing_happened\nUNMANAGED forged'
    payload = json.dumps(
        {
            "session_id": hostile,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
    )
    _run(REGISTRATION_GUARD, payload, tmp_path, AGENTSTACK_MCP_URL=UNREACHABLE)
    log = tmp_path / "runtime" / "logs" / "unmanaged_sessions.jsonl"
    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1, "the payload wrote a log line of its own"
    assert json.loads(lines[0])["session_id"] == hostile


def test_the_first_warning_is_claimed_once_under_concurrency(tmp_path: Path) -> None:
    """Matching hooks run in parallel, so "check then create" is not a claim."""
    import concurrent.futures

    payload = _registration_payload("parallel-1")
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(
            pool.map(
                lambda _: _run(
                    REGISTRATION_GUARD, payload, tmp_path, AGENTSTACK_MCP_URL=UNREACHABLE
                ),
                range(12),
            )
        )
    warned = [r for r in results if "systemMessage" in r.stdout]
    assert all(r.returncode == 0 for r in results)
    assert len(warned) == 1, f"{len(warned)} of 12 calls each believed it was the first"


# --- registering somebody else does not register you ---------------------

MARK_HOOK = REPO_ROOT / "hooks" / "mark-agent-registered.sh"


def _fake_tmux(tmp_path: Path, session_name: str) -> Path:
    """A tmux that reports one session name, so identity resolution has a source."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    tmux = bin_dir / "tmux"
    tmux.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "display-message" ]; then\n'
        f'  printf "%s\\n" "{session_name}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    tmux.chmod(0o755)
    return bin_dir


def _run_mark(tmp_path: Path, payload: str, **env: str) -> subprocess.CompletedProcess:
    environment = dict(BASE_ENV)
    environment["HOME"] = str(tmp_path / "home")
    environment["AGENTSTACK_RUNTIME_DIR"] = str(tmp_path / "runtime")
    environment["AGENTSTACK_HOOKS_DIR"] = str(REPO_ROOT / "hooks")
    Path(environment["HOME"]).mkdir(parents=True, exist_ok=True)
    environment.update(env)
    return subprocess.run(
        ["/bin/bash", str(MARK_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )


def test_registering_a_child_does_not_register_the_parent_session(tmp_path: Path, answering_endpoint: str) -> None:
    """The flag follows the binding, not the tool call.

    Refusing to write the record was only half of it: the flag was still
    created, so a session could clear its own registration guard by registering
    somebody else and never registering itself.
    """
    session_id = "parent-tmux-1"
    flag = Path("/tmp") / f".claude-agent-registered-{session_id}"
    flag.unlink(missing_ok=True)
    bin_dir = _fake_tmux(tmp_path, "ParentTmux")
    payload = json.dumps(
        {
            "session_id": session_id,
            "transcript_path": str(tmp_path / "t.jsonl"),
            "cwd": str(tmp_path),
            "tool_input": {"name": "ChildAgent", "project_key": str(tmp_path)},
            "tool_response": {"id": 91, "name": "ChildAgent"},
        }
    )
    try:
        before = _run(REGISTRATION_GUARD, _registration_payload(session_id), tmp_path,
                      PATH=f"{bin_dir}:{BASE_ENV['PATH']}", TMUX="/tmp/x,1,0", TMUX_PANE="%1",
                      AGENTSTACK_MCP_URL=answering_endpoint)
        assert before.returncode == 2

        result = _run_mark(
            tmp_path, payload, PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
            TMUX="/tmp/x,1,0", TMUX_PANE="%1",
            AGENTSTACK_MCP_URL=answering_endpoint,
        )
        assert result.returncode == 0, result.stderr  # the child's registration still stands

        index = tmp_path / "runtime" / "session_index"
        assert not list(index.glob("*.json")) if index.exists() else True
        assert not flag.exists(), "registering a child registered the parent"

        after = _run(REGISTRATION_GUARD, _registration_payload(session_id), tmp_path,
                     PATH=f"{bin_dir}:{BASE_ENV['PATH']}", TMUX="/tmp/x,1,0", TMUX_PANE="%1",
                     AGENTSTACK_MCP_URL=answering_endpoint)
        assert after.returncode == 2, "the guard was cleared without a binding"
    finally:
        flag.unlink(missing_ok=True)


def test_registering_yourself_does_create_the_flag(tmp_path: Path, answering_endpoint: str) -> None:
    """The null case: the ordinary path must still work.

    Without this, "never create the flag" would pass the test above and break
    every session.
    """
    session_id = "self-tmux-1"
    flag = Path("/tmp") / f".claude-agent-registered-{session_id}"
    flag.unlink(missing_ok=True)
    bin_dir = _fake_tmux(tmp_path, "SelfAgent")
    payload = json.dumps(
        {
            "session_id": session_id,
            "transcript_path": str(tmp_path / "t.jsonl"),
            "cwd": str(tmp_path),
            "tool_input": {"name": "SelfAgent", "project_key": str(tmp_path)},
            "tool_response": {"id": 92, "name": "SelfAgent"},
        }
    )
    try:
        result = _run_mark(
            tmp_path, payload, PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
            TMUX="/tmp/x,1,0", TMUX_PANE="%1",
            AGENTSTACK_MCP_URL=answering_endpoint,
        )
        assert result.returncode == 0, result.stderr
        assert flag.exists(), "a self registration did not register the session"
        assert (tmp_path / "runtime" / "session_index" / "92.json").exists()
    finally:
        flag.unlink(missing_ok=True)


def test_a_binding_that_cannot_be_written_does_not_register_the_session(
    tmp_path: Path,
) -> None:
    """A flag with no binding is the state both guards misread."""
    session_id = "unwritable-1"
    flag = Path("/tmp") / f".claude-agent-registered-{session_id}"
    flag.unlink(missing_ok=True)
    blocked = tmp_path / "runtime" / "session_index"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("not a directory", encoding="utf-8")
    payload = json.dumps(
        {
            "session_id": session_id,
            "tool_input": {"name": "SoloAgent", "project_key": str(tmp_path)},
            "tool_response": {"id": 93, "name": "SoloAgent"},
        }
    )
    try:
        result = _run_mark(tmp_path, payload)
        assert result.returncode == 0
        assert not flag.exists(), "the session was registered without a binding"
        assert "SESSION BINDING NOT WRITTEN" in result.stderr
    finally:
        flag.unlink(missing_ok=True)


# --- the session start reminder knows who a bound session is -------------

SESSION_START = REPO_ROOT / "hooks" / "session-start-reminder.sh"


def test_the_session_start_reminder_finds_an_existing_binding(tmp_path: Path) -> None:
    """A resumed session is not a stranger.

    The reminder resolves identity the same way the guards do, but it was
    reading only the environment and tmux, so a client identified by its
    session binding was told it had no identity and invited to register a new
    one -- the way duplicate agents get created.
    """
    project = str(tmp_path / "project")
    _record_registration(tmp_path, "resume-1", 41, "IcyGauss", project)
    environment = dict(BASE_ENV)
    environment["HOME"] = str(tmp_path / "home")
    environment["AGENTSTACK_RUNTIME_DIR"] = str(tmp_path / "runtime")
    environment["AGENTSTACK_HOOKS_DIR"] = str(REPO_ROOT / "hooks")
    environment["AGENTSTACK_MCP_URL"] = UNREACHABLE
    Path(environment["HOME"]).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["/bin/bash", str(SESSION_START)],
        input=json.dumps({"session_id": "resume-1", "hook_event_name": "SessionStart"}),
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "IcyGauss" in result.stdout, result.stdout


def test_the_reminder_does_not_invent_an_identity(tmp_path: Path) -> None:
    """The null case: an unknown session is still unknown."""
    project = str(tmp_path / "project")
    _record_registration(tmp_path, "resume-1", 41, "IcyGauss", project)
    environment = dict(BASE_ENV)
    environment["HOME"] = str(tmp_path / "home")
    environment["AGENTSTACK_RUNTIME_DIR"] = str(tmp_path / "runtime")
    environment["AGENTSTACK_HOOKS_DIR"] = str(REPO_ROOT / "hooks")
    environment["AGENTSTACK_MCP_URL"] = UNREACHABLE
    Path(environment["HOME"]).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["/bin/bash", str(SESSION_START)],
        input=json.dumps({"session_id": "somebody-else", "hook_event_name": "SessionStart"}),
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "IcyGauss" not in result.stdout, result.stdout


# --- what counts as "the service is there" -------------------------------


@pytest.fixture()
def hanging_endpoint():
    """A listener that accepts and then says nothing.

    A TCP connection proves a socket is open, not that anything will answer. A
    session cannot register against this either, so both guards must read it the
    same way -- one of them calling it reachable while the other times out is
    how the two disagreed about the same endpoint.
    """
    import socket
    import threading

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    accepted = []
    stop = threading.Event()

    def accept_forever():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            accepted.append(conn)

    thread = threading.Thread(target=accept_forever, daemon=True)
    thread.start()
    try:
        host, port = server.getsockname()
        yield f"http://{host}:{port}/api/"
    finally:
        stop.set()
        thread.join(timeout=2)
        for conn in accepted:
            conn.close()
        server.close()


def test_a_listener_that_never_answers_is_not_a_running_service(
    tmp_path: Path, hanging_endpoint: str
) -> None:
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("hang-1"),
        tmp_path,
        AGENTSTACK_MCP_URL=hanging_endpoint,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "systemMessage" in result.stdout


def test_both_guards_read_a_hanging_listener_the_same_way(
    tmp_path: Path, hanging_endpoint: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.md"
    target.write_text("x", encoding="utf-8")
    registration = _run(
        REGISTRATION_GUARD, _registration_payload("hang-2"), tmp_path,
        AGENTSTACK_MCP_URL=hanging_endpoint, AGENTSTACK_MAIL_OUTAGE_POLICY="block",
    )
    reservation = _run(
        RESERVATION_GUARD, _edit_payload("hang-2", target), tmp_path,
        AGENTSTACK_PROTECTED_ROOTS=str(project),
        AGENTSTACK_MCP_URL=hanging_endpoint, AGENTSTACK_MAIL_OUTAGE_POLICY="block",
    )
    assert registration.returncode == reservation.returncode == 2, (
        registration.stderr, reservation.stderr
    )


@pytest.mark.parametrize("endpoint", ["not-a-url", "", "ftp://127.0.0.1/api", "http:///api"])
def test_an_endpoint_that_is_not_an_address_is_refused_not_excused(
    tmp_path: Path, endpoint: str
) -> None:
    """A typo in the address of the authority must not remove the authority.

    Parsing loosely turned "not-a-url" into localhost:80, found nothing there,
    and called it an outage -- so a misconfiguration opened the guards.
    """
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("bad-endpoint"),
        tmp_path,
        AGENTSTACK_MCP_URL=endpoint,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "ENDPOINT UNUSABLE" in result.stderr
    log = tmp_path / "runtime" / "logs" / "unmanaged_sessions.jsonl"
    assert not log.exists(), "a misconfiguration was recorded as an outage"


def test_a_client_without_the_launcher_uses_the_installed_endpoint(tmp_path: Path) -> None:
    """The raw client inherits none of the installer's environment.

    Falling back to a fixed port would ask about a service this install may not
    run: a native install elsewhere would look like an outage and open the
    guards while the real service was up.
    """
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):  # noqa: N802 - stdlib naming
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        agentstack_home = tmp_path / "installed"
        agentstack_home.mkdir()
        (agentstack_home / "env.sh").write_text(
            f'export AGENTSTACK_MCP_URL=http://{host}:{port}/mcp\n', encoding="utf-8"
        )
        result = _run(
            REGISTRATION_GUARD,
            _registration_payload("installed-1"),
            tmp_path,
            AGENTSTACK_HOME=str(agentstack_home),
            # Explicitly absent: this is the whole point of the test.
            AGENTSTACK_MCP_URL="",
        )
        # The installed endpoint answers, so registration is possible and required.
        assert result.returncode == 2, result.stdout + result.stderr
        assert "UNMANAGED SESSION BLOCKED" in result.stderr
    finally:
        server.shutdown()
        server.server_close()


def test_a_conflicting_identity_is_refused_by_the_bash_guard_during_an_outage(
    tmp_path: Path,
) -> None:
    """Bash can write any file, so the ambiguity has to stop here as well."""
    project = str(tmp_path / "project")
    _bind(tmp_path, 41, "bash-conflict", "FirstName", project)
    _bind(tmp_path, 77, "bash-conflict", "SecondName", project)
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("bash-conflict"),
        tmp_path,
        AGENTSTACK_PROJECT_KEY=project,
        AGENTSTACK_MCP_URL=UNREACHABLE,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "AGENT IDENTITY CONFLICT" in result.stderr


def test_the_warning_does_not_repeat_the_credentials_in_the_endpoint(tmp_path: Path) -> None:
    """The warning is shown to a person; the URL may carry a secret."""
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("secret-1"),
        tmp_path,
        AGENTSTACK_MCP_URL="http://user:hunter2@127.0.0.1:1/api/?token=alsosecret",
    )
    assert result.returncode == 0, result.stderr
    assert "hunter2" not in result.stdout
    assert "alsosecret" not in result.stdout
    assert "127.0.0.1" in result.stdout


# --- the round-5 findings ------------------------------------------------


@pytest.fixture()
def redirecting_endpoint():
    """An endpoint that answers with a redirect to somewhere dead."""
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):  # noqa: N802 - stdlib naming
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/no-service")
            self.end_headers()

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}/api/"
    finally:
        server.shutdown()
        server.server_close()


def test_a_redirect_is_an_answer(tmp_path: Path, redirecting_endpoint: str) -> None:
    """Something replied. Where it points is a different question.

    Following the redirect made the verdict depend on the target, so a
    canonical-slash redirect in front of a dead upstream read as "the service
    is gone" and opened the guards.
    """
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("redirect-1"),
        tmp_path,
        AGENTSTACK_MCP_URL=redirecting_endpoint,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "UNMANAGED SESSION BLOCKED" in result.stderr


def test_a_named_session_is_not_exempt_from_the_outage_policy(tmp_path: Path) -> None:
    """Having a name is not a licence to write while coordination is down.

    The AGENT_NAME exemption existed so a bot could run Bash to re-register.
    It had grown into an exemption from every check, and Bash can write any
    file, so `block` stopped only the sessions that happened to be anonymous.
    """
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("named-outage"),
        tmp_path,
        AGENT_NAME="IcyGauss",
        AGENTSTACK_MCP_URL=UNREACHABLE,
        AGENTSTACK_MAIL_OUTAGE_POLICY="block",
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "AGENT MAIL UNREACHABLE" in result.stderr


def test_a_named_session_still_works_when_the_policy_allows_it(
    tmp_path: Path, answering_endpoint: str
) -> None:
    """The null case: the exemption it does have must survive.

    A channels bot has AGENT_NAME and no flag; it must still be able to run
    Bash, or it cannot re-register itself after a /clear.
    """
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("named-healthy"),
        tmp_path,
        AGENT_NAME="IcyGauss",
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_conflict_is_refused_even_after_the_session_registered(
    tmp_path: Path, answering_endpoint: str
) -> None:
    """A binding can become ambiguous later, and the flag does not expire."""
    session_id = "late-conflict"
    project = str(tmp_path / "project")
    flag = Path("/tmp") / f".claude-agent-registered-{session_id}"
    flag.write_text("", encoding="utf-8")
    try:
        _bind(tmp_path, 41, session_id, "FirstName", project)
        _bind(tmp_path, 77, session_id, "SecondName", project)
        result = _run(
            REGISTRATION_GUARD,
            _registration_payload(session_id),
            tmp_path,
            AGENTSTACK_PROJECT_KEY=project,
            AGENTSTACK_MCP_URL=answering_endpoint,
        )
        assert result.returncode == 2, result.stdout + result.stderr
        assert "AGENT IDENTITY CONFLICT" in result.stderr
    finally:
        flag.unlink(missing_ok=True)


def test_the_project_comes_from_the_payload_when_nothing_else_says(
    tmp_path: Path, answering_endpoint: str
) -> None:
    """A client without the launcher carries its project only as cwd.

    Without reading it, the lookup ranges over every project's bindings, and
    two unrelated sessions that happen to share an id look like one ambiguous
    session -- a conflict invented out of scope, not observed.
    """
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    _record_registration(tmp_path, "scoped-1", 41, "AgentInA", str(project_a))
    _record_registration(tmp_path, "scoped-1", 77, "AgentInB", str(project_b))
    payload = json.dumps(
        {
            "session_id": "scoped-1",
            "cwd": str(project_a),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
    )
    result = _run(REGISTRATION_GUARD, payload, tmp_path, AGENTSTACK_MCP_URL=answering_endpoint)
    assert "AGENT IDENTITY CONFLICT" not in result.stderr, result.stderr


def test_both_guards_ask_about_the_same_endpoint(tmp_path: Path) -> None:
    """One installation, one endpoint.

    The reservation guard fixed its own endpoint before the shared resolver
    ran, so on a custom-port install it probed the legacy default while the
    registration guard read the installed one. A session could pass one guard
    and be refused by the other, inside a single tool call.
    """
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.md"
    target.write_text("x", encoding="utf-8")
    agentstack_home = tmp_path / "installed"
    agentstack_home.mkdir()
    # An endpoint nothing is listening on: both guards must call it an outage.
    (agentstack_home / "env.sh").write_text(
        "export AGENTSTACK_MCP_URL=http://127.0.0.1:1/mcp\n", encoding="utf-8"
    )
    common = dict(AGENTSTACK_HOME=str(agentstack_home), AGENTSTACK_MCP_URL="")
    registration = _run(REGISTRATION_GUARD, _registration_payload("shared-1"), tmp_path, **common)
    reservation = _run(
        RESERVATION_GUARD,
        _edit_payload("shared-1", target),
        tmp_path,
        AGENTSTACK_PROTECTED_ROOTS=str(project),
        **common,
    )
    assert registration.returncode == reservation.returncode == 0, (
        registration.stderr,
        reservation.stderr,
    )


# --- the round-6 findings ------------------------------------------------


def test_a_registered_session_without_a_binding_is_not_asked_to_re_register_during_an_outage(
    tmp_path: Path,
) -> None:
    """The original dead end, in its last hiding place.

    "Call register_agent again" cannot be done while the service is down. The
    reservation guard asked for it before it asked whether the service was
    there, so a session that had registered earlier was trapped by the guard
    the other one had just waved through.
    """
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.md"
    target.write_text("x", encoding="utf-8")
    session_id = "flagged-outage"
    flag = Path("/tmp") / f".claude-agent-registered-{session_id}"
    flag.write_text("", encoding="utf-8")
    try:
        common = dict(
            AGENTSTACK_PROTECTED_ROOTS=str(project),
            AGENTSTACK_PROJECT_KEY=str(project),
            AGENTSTACK_MCP_URL=UNREACHABLE,
        )
        registration = _run(
            REGISTRATION_GUARD, _registration_payload(session_id), tmp_path, **common
        )
        reservation = _run(
            RESERVATION_GUARD, _edit_payload(session_id, target), tmp_path, **common
        )
        assert registration.returncode == 0, registration.stderr
        assert reservation.returncode == 0, reservation.stderr

        blocked = _run(
            RESERVATION_GUARD,
            _edit_payload(session_id, target),
            tmp_path,
            AGENTSTACK_MAIL_OUTAGE_POLICY="block",
            **common,
        )
        assert blocked.returncode == 2
        assert "AGENT MAIL UNREACHABLE" in blocked.stderr
    finally:
        flag.unlink(missing_ok=True)


def test_no_endpoint_at_all_is_refused_by_both_guards(tmp_path: Path) -> None:
    """The other side of "use the installed endpoint".

    One guard called a missing configuration unusable while the other quietly
    substituted the legacy port -- so on a machine with no endpoint configured,
    the Edit guard alone stayed open, pointed at a service that may not be this
    install's.
    """
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.md"
    target.write_text("x", encoding="utf-8")
    agentstack_home = tmp_path / "installed"
    agentstack_home.mkdir()  # deliberately no env.sh
    common = dict(AGENTSTACK_HOME=str(agentstack_home), AGENTSTACK_MCP_URL="")
    registration = _run(REGISTRATION_GUARD, _registration_payload("noconf-1"), tmp_path, **common)
    reservation = _run(
        RESERVATION_GUARD,
        _edit_payload("noconf-1", target),
        tmp_path,
        AGENTSTACK_PROTECTED_ROOTS=str(project),
        **common,
    )
    assert registration.returncode == 2, registration.stdout + registration.stderr
    assert reservation.returncode == 2, reservation.stdout + reservation.stderr
    assert "ENDPOINT UNUSABLE" in registration.stderr
    assert "ENDPOINT UNUSABLE" in reservation.stderr


def test_a_named_session_is_still_checked_for_conflicts(
    tmp_path: Path, answering_endpoint: str
) -> None:
    """Moving the check earlier was not enough; it was asking the wrong thing.

    The precedence resolver returns as soon as AGENT_NAME is set, so routing
    the conflict question through it meant a named session's bindings were
    never read -- the check ran, looked at nothing, and reported no conflict.
    """
    project = str(tmp_path / "project")
    _bind(tmp_path, 41, "named-conflict", "FirstName", project)
    _bind(tmp_path, 77, "named-conflict", "SecondName", project)
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("named-conflict"),
        tmp_path,
        AGENT_NAME="FirstName",
        AGENTSTACK_PROJECT_KEY=project,
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "AGENT IDENTITY CONFLICT" in result.stderr


def test_a_launcher_name_that_disagrees_with_the_binding_is_a_conflict(
    tmp_path: Path, answering_endpoint: str
) -> None:
    """Two claims are two claims, whichever one would have won.

    An environment saying one name while the session registered as another
    means one of them writes under the other's name.
    """
    project = str(tmp_path / "project")
    _record_registration(tmp_path, "env-vs-index", 41, "RegisteredName", project)
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("env-vs-index"),
        tmp_path,
        AGENT_NAME="SomeOtherName",
        AGENTSTACK_PROJECT_KEY=project,
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "AGENT IDENTITY CONFLICT" in result.stderr


def test_a_launcher_name_that_agrees_is_not_a_conflict(
    tmp_path: Path, answering_endpoint: str
) -> None:
    """The null case: the ordinary child agent must keep working."""
    project = str(tmp_path / "project")
    _record_registration(tmp_path, "env-matches", 41, "SameName", project)
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("env-matches"),
        tmp_path,
        AGENT_NAME="SameName",
        AGENTSTACK_PROJECT_KEY=project,
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- the round-7 findings ------------------------------------------------


def _fake_tmux_session(tmp_path: Path, session_name: str) -> Path:
    return _fake_tmux(tmp_path, session_name)


def test_a_launcher_name_that_disagrees_with_the_tmux_session_is_a_conflict(
    tmp_path: Path,
) -> None:
    """Every source is a claim, including the one the resolver never reaches.

    The scan collected the environment and the index but not the tmux session
    this pane is actually in, and the precedence resolver returns on
    AGENT_NAME -- so a session whose launcher name disagreed with its tmux
    session was compared by nobody, and an outage let it write under either.
    """
    bin_dir = _fake_tmux_session(tmp_path, "TmuxClaim")
    for guard, payload in (
        (REGISTRATION_GUARD, _registration_payload("tmux-conflict")),
        (REGISTRATION_GUARD, _registration_payload("tmux-conflict")),
    ):
        result = _run(
            guard,
            payload,
            tmp_path,
            AGENT_NAME="EnvClaim",
            TMUX="/tmp/x,1,0",
            TMUX_PANE="%9",
            PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
            # Even during an outage: an outage is not an escape from ambiguity.
            AGENTSTACK_MCP_URL=UNREACHABLE,
        )
        assert result.returncode == 2, result.stdout + result.stderr
        assert "AGENT IDENTITY CONFLICT" in result.stderr


def test_the_edit_guard_agrees_about_the_tmux_conflict(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.md"
    target.write_text("x", encoding="utf-8")
    bin_dir = _fake_tmux_session(tmp_path, "TmuxClaim")
    result = _run(
        RESERVATION_GUARD,
        _edit_payload("tmux-conflict-2", target),
        tmp_path,
        AGENT_NAME="EnvClaim",
        TMUX="/tmp/x,1,0",
        TMUX_PANE="%9",
        PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
        AGENTSTACK_PROTECTED_ROOTS=str(project),
        AGENTSTACK_MCP_URL=UNREACHABLE,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "AGENT IDENTITY CONFLICT" in result.stderr


def test_a_launcher_name_matching_the_tmux_session_is_not_a_conflict(
    tmp_path: Path,
) -> None:
    """The null case: the ordinary launched agent must keep working."""
    bin_dir = _fake_tmux_session(tmp_path, "SameClaim")
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("tmux-agrees"),
        tmp_path,
        AGENT_NAME="SameClaim",
        TMUX="/tmp/x,1,0",
        TMUX_PANE="%9",
        PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
        AGENTSTACK_MCP_URL=UNREACHABLE,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_placeholder_name_is_not_a_claim(tmp_path: Path, answering_endpoint: str) -> None:
    """"pending-1234" is a session that has not been named yet, not an agent.

    Three places had their own idea of what counts as an identity, so the same
    value was a claim in one and nothing in another -- which made a real
    binding look like a conflict.
    """
    project = str(tmp_path / "project")
    _record_registration(tmp_path, "placeholder-1", 41, "RealName", project)
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("placeholder-1"),
        tmp_path,
        AGENT_NAME="pending-1234",
        AGENTSTACK_PROJECT_KEY=project,
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert "AGENT IDENTITY CONFLICT" not in result.stderr, result.stderr


def test_a_placeholder_name_does_not_exempt_a_session_from_registering(
    tmp_path: Path, answering_endpoint: str
) -> None:
    """"pending-1234" is not an agent, so it cannot stand in for one.

    The exemption exists so a named bot can run Bash to re-register itself.
    Accepting any non-empty value turned it into "anything the environment
    happens to contain", which is how an unnamed session got the rights of a
    named one.
    """
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("placeholder-exempt"),
        tmp_path,
        AGENT_NAME="pending-4820",
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 2, result.stdout + result.stderr


def test_a_real_name_still_exempts(tmp_path: Path, answering_endpoint: str) -> None:
    """The null case: the exemption the bots actually rely on."""
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("real-exempt"),
        tmp_path,
        AGENT_NAME="ProOpus",
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_audit_log_does_not_grow_without_bound(tmp_path: Path) -> None:
    """An audit trail that fills the disk stops being one.

    This machine already produced a 677 MB service log by appending forever.
    """
    log = tmp_path / "runtime" / "logs" / "unmanaged_sessions.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("x" * 4096, encoding="utf-8")
    _run(
        REGISTRATION_GUARD,
        _registration_payload("rotate-1"),
        tmp_path,
        AGENTSTACK_MCP_URL=UNREACHABLE,
        AGENTSTACK_AUDIT_MAX_BYTES="1024",
    )
    assert log.exists()
    assert log.stat().st_size < 4096, "the oversized log was appended to rather than rotated"
    assert (tmp_path / "runtime" / "logs" / "unmanaged_sessions.jsonl.1").exists(), (
        "the previous generation was discarded instead of kept"
    )


@pytest.mark.parametrize("guard", ["registration", "reservation"])
def test_stale_pane_metadata_alone_does_not_block_either_guard(
    tmp_path: Path, guard: str
) -> None:
    """Both guards, because this regressed in the resolver they share."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "agent_name__9").write_text("StaleAgent\n", encoding="utf-8")
    common = dict(
        AGENT_NAME="EnvClaim",
        TMUX="/tmp/x,1,0",
        TMUX_PANE="%9",
        AGENTSTACK_MCP_URL=UNREACHABLE,
    )
    if guard == "registration":
        result = _run(
            REGISTRATION_GUARD, _registration_payload("stale-meta-both"), tmp_path, **common
        )
    else:
        project = tmp_path / "project"
        project.mkdir()
        target = project / "note.md"
        target.write_text("x", encoding="utf-8")
        result = _run(
            RESERVATION_GUARD,
            _edit_payload("stale-meta-both", target),
            tmp_path,
            AGENTSTACK_PROTECTED_ROOTS=str(project),
            **common,
        )
    assert "AGENT IDENTITY CONFLICT" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stdout + result.stderr


def test_stale_pane_metadata_alone_does_not_block(tmp_path: Path) -> None:
    """Metadata corroborates a tmux session; it does not claim one.

    A metadata file outlives the pane it describes. Promoting a stale one to an
    independent claim refused a session whose identity was not in doubt, and
    the refusal pointed at bindings and environment variables the operator
    would find nothing wrong with -- a new dead end in the shape of the old one.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "agent_name__9").write_text("StaleAgent\n", encoding="utf-8")
    # tmux cannot be reached here, so there is no session to corroborate.
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("stale-meta"),
        tmp_path,
        AGENT_NAME="EnvClaim",
        TMUX="/tmp/x,1,0",
        TMUX_PANE="%9",
        AGENTSTACK_MCP_URL=UNREACHABLE,
    )
    assert "AGENT IDENTITY CONFLICT" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stdout + result.stderr


def test_metadata_that_contradicts_a_live_tmux_session_is_still_a_conflict(
    tmp_path: Path,
) -> None:
    """The null case: corroboration that contradicts is exactly the signal."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "agent_name__9").write_text("StaleAgent\n", encoding="utf-8")
    bin_dir = _fake_tmux_session(tmp_path, "LiveSession")
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("meta-vs-tmux"),
        tmp_path,
        AGENT_NAME="LiveSession",
        TMUX="/tmp/x,1,0",
        TMUX_PANE="%9",
        PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
        AGENTSTACK_MCP_URL=UNREACHABLE,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "AGENT IDENTITY CONFLICT" in result.stderr
    assert "agent_name_" in result.stderr, "the refusal did not mention the metadata"


# --- the round-9 findings ------------------------------------------------


@pytest.mark.parametrize("guard", ["registration", "reservation"])
def test_placeholder_pane_metadata_does_not_conflict_with_a_live_session(
    tmp_path: Path, guard: str
) -> None:
    """Both guards, because the resolver is where this last regressed.

    The shared scan filtered placeholders; the resolver kept its own copy and
    did not apply it to pane metadata at all. So "pending-1234" in a metadata
    file contradicted a real tmux identity and refused the session -- before
    the transport policy, and before any reservation was even checked.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "agent_name__9").write_text("pending-1234\n", encoding="utf-8")
    bin_dir = _fake_tmux_session(tmp_path, "LiveAgent")
    common = dict(
        TMUX="/tmp/x,1,0",
        TMUX_PANE="%9",
        PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
        AGENTSTACK_MCP_URL=UNREACHABLE,
    )
    if guard == "registration":
        result = _run(
            REGISTRATION_GUARD, _registration_payload("placeholder-meta"), tmp_path, **common
        )
    else:
        project = tmp_path / "project"
        project.mkdir()
        target = project / "note.md"
        target.write_text("x", encoding="utf-8")
        result = _run(
            RESERVATION_GUARD,
            _edit_payload("placeholder-meta", target),
            tmp_path,
            AGENTSTACK_PROTECTED_ROOTS=str(project),
            **common,
        )
    assert "AGENT IDENTITY CONFLICT" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_placeholder_name_leaves_the_session_unmanaged(
    tmp_path: Path, answering_endpoint: str
) -> None:
    """One predicate, or the same value means two things.

    "pending-1234" was not an identity when deciding conflicts and exemptions,
    but counted as an identity source when deciding whether the unmanaged
    policy applied -- so the explicit opt-out could not reach the sessions that
    needed it.
    """
    result = _run(
        REGISTRATION_GUARD,
        _registration_payload("placeholder-unmanaged"),
        tmp_path,
        AGENT_NAME="pending-1234",
        AGENTSTACK_UNMANAGED_SESSION_POLICY="warn-open",
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_retained_generation_survives_concurrent_rotation(tmp_path: Path) -> None:
    """Rotation has to be one decision, not three steps two writers interleave.

    Matching hooks run in parallel. With a size check, a move and an append as
    separate operations, two writers both decided to rotate and the second
    moved the first's fresh log over the generation it was meant to keep.
    """
    import concurrent.futures

    log = tmp_path / "runtime" / "logs" / "unmanaged_sessions.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    sentinel = '{"event": "sentinel"}\n'
    # Sized so that exactly one rotation is warranted: the log is over the
    # limit, and what the writers add afterwards stays well under it. More than
    # one rotation here would be the race, not the policy.
    log.write_text(sentinel + "x" * 9000, encoding="utf-8")

    def one(index: int):
        return _run(
            REGISTRATION_GUARD,
            _registration_payload(f"rotate-race-{index}"),
            tmp_path,
            AGENTSTACK_MCP_URL=UNREACHABLE,
            AGENTSTACK_AUDIT_MAX_BYTES="8192",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, range(8)))

    kept = tmp_path / "runtime" / "logs" / "unmanaged_sessions.jsonl.1"
    assert kept.exists(), "the retained generation is gone"
    assert "sentinel" in kept.read_text(encoding="utf-8"), (
        "the retained generation is not the history it replaced"
    )


@pytest.mark.parametrize("value", ["not-a-number", "-1", "0", ""])
def test_a_nonsense_audit_limit_does_not_disable_rotation(tmp_path: Path, value: str) -> None:
    """A misconfigured limit must not mean "never" or "every call"."""
    log = tmp_path / "runtime" / "logs" / "unmanaged_sessions.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("small\n", encoding="utf-8")
    _run(
        REGISTRATION_GUARD,
        _registration_payload(f"limit-{value or 'empty'}"),
        tmp_path,
        AGENTSTACK_MCP_URL=UNREACHABLE,
        AGENTSTACK_AUDIT_MAX_BYTES=value,
    )
    assert not (tmp_path / "runtime" / "logs" / "unmanaged_sessions.jsonl.1").exists(), (
        f"{value!r} rotated a small log"
    )
    assert "small" in log.read_text(encoding="utf-8")


# --- the round-10 findings -----------------------------------------------


def test_a_placeholder_env_does_not_hide_a_live_tmux_identity(tmp_path: Path) -> None:
    """A value that is not an identity must not win the precedence either.

    The resolver returned as soon as AGENT_NAME was set, placeholder or not, so
    "pending-1234" left in the environment made a real tmux identity -- or a
    session that had registered perfectly well -- unresolvable.
    """
    bin_dir = _fake_tmux_session(tmp_path, "LiveAgent")
    assert (
        _resolve(
            tmp_path,
            "placeholder-shadow",
            AGENT_NAME="pending-1234",
            TMUX="/tmp/x,1,0",
            TMUX_PANE="%9",
            PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
        )
        == "LiveAgent|tmux-session"
    )


def test_a_placeholder_env_does_not_hide_a_registered_identity(tmp_path: Path) -> None:
    project = str(tmp_path / "project")
    _record_registration(tmp_path, "placeholder-binding", 41, "BoundAgent", project)
    assert (
        _resolve(
            tmp_path,
            "placeholder-binding",
            AGENT_NAME="pending-1234",
            AGENTSTACK_LOOKUP_PROJECT_KEY=project,
        )
        == "BoundAgent|session-index"
    )


@pytest.mark.parametrize(
    ("label", "tmux_env"),
    [
        ("a pane still called pending-*", {"TMUX": "/tmp/x,1,0", "TMUX_PANE": "%9", "_session": "pending-1234"}),
        ("TMUX inherited without a pane", {"TMUX": "/tmp/x,1,0"}),
        ("a pane whose session cannot be read", {"TMUX": "/tmp/x,1,0", "TMUX_PANE": "%9"}),
    ],
)
@pytest.mark.parametrize("guard", ["registration", "reservation"])
def test_unresolved_tmux_state_is_not_an_identity_source(
    tmp_path: Path, answering_endpoint: str, label: str, tmux_env: dict, guard: str
) -> None:
    """Being inside tmux is not an identity.

    Counting any tmux variable as a source classed these sessions as managed,
    so the explicit opt-out could not reach the state they are actually in --
    which is the common one right after a launcher starts a pane.
    """
    env = dict(tmux_env)
    session = env.pop("_session", None)
    if session:
        env["PATH"] = f"{_fake_tmux_session(tmp_path, session)}:{BASE_ENV['PATH']}"
    env["AGENTSTACK_UNMANAGED_SESSION_POLICY"] = "warn-open"
    env["AGENTSTACK_MCP_URL"] = answering_endpoint
    if guard == "registration":
        result = _run(REGISTRATION_GUARD, _registration_payload("tmux-unresolved"), tmp_path, **env)
    else:
        project = tmp_path / "project"
        project.mkdir()
        target = project / "note.md"
        target.write_text("x", encoding="utf-8")
        result = _run(
            RESERVATION_GUARD,
            _edit_payload("tmux-unresolved", target),
            tmp_path,
            AGENTSTACK_PROTECTED_ROOTS=str(project),
            **env,
        )
    assert result.returncode == 0, f"{label}: {result.stdout + result.stderr}"


@pytest.mark.parametrize("guard", ["registration", "reservation"])
def test_a_real_exact_session_is_still_an_identity_source(
    tmp_path: Path, answering_endpoint: str, guard: str
) -> None:
    """The null case: a launched agent is managed and must register."""
    bin_dir = _fake_tmux_session(tmp_path, "RealAgent")
    env = dict(
        TMUX="/tmp/x,1,0",
        TMUX_PANE="%9",
        PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
        AGENTSTACK_UNMANAGED_SESSION_POLICY="warn-open",
        AGENTSTACK_MCP_URL=answering_endpoint,
    )
    if guard == "registration":
        result = _run(REGISTRATION_GUARD, _registration_payload("tmux-real"), tmp_path, **env)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "AGENT NOT REGISTERED" in result.stderr
    else:
        project = tmp_path / "project"
        project.mkdir()
        target = project / "note.md"
        target.write_text("x", encoding="utf-8")
        result = _run(
            RESERVATION_GUARD,
            _edit_payload("tmux-real", target),
            tmp_path,
            AGENTSTACK_PROTECTED_ROOTS=str(project),
            **env,
        )
        # It has an identity, so it is held to the reservation check.
        assert "UNMANAGED SESSION" not in result.stderr, result.stderr
