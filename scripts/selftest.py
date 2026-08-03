#!/usr/bin/env python3
"""Prove the installed stack works, rather than that its parts are present.

``agentstack-doctor`` answers "is it there?". Every failure this project has
shipped to a tester answered that question with yes: a launchd job registered
but dead, a database path recorded but absent, a dashboard installed but
holding a stale process. So this asks the other question — put two agents in
the system, have them find each other, exchange messages, take a lock, and
check that the dashboard can see all of it.

Nothing here uses a private path. It talks to the same agent-mail endpoint the
hooks use and reads the same HTTP API the browser reads, because a self-test
that runs somewhere the product does not is a test of the self-test.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

RESERVED_PATH = ".agentstack-selftest/lock-probe.txt"


class Fail(Exception):
    """A step that decides the installation is not working."""


class Reporter:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.step = 0

    def ok(self, message: str) -> None:
        self.step += 1
        print(f"ok {self.step}: {message}")

    def fail(self, message: str, detail: str = "") -> None:
        self.step += 1
        print(f"FAIL {self.step}: {message}", file=sys.stderr)
        if detail:
            print(f"      {detail.strip()}", file=sys.stderr)
        self.failures.append(message)

    def note(self, message: str) -> None:
        print(f"    {message}")


def load_env(install_dir: pathlib.Path) -> dict[str, str]:
    """Read the installed env.sh the way every other component does."""
    env_file = install_dir / "env.sh"
    if not env_file.is_file():
        raise Fail(
            f"no installed environment at {env_file}; run scripts/install.sh first"
        )
    # env.sh is generated shell. Sourcing it in a subshell is closer to what the
    # hooks do than re-implementing a parser that would drift from the writer.
    result = subprocess.run(
        ["bash", "-c", f'set -a; . "{env_file}"; env'],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise Fail(f"could not read {env_file}: {result.stderr.strip()}")
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key.startswith("AGENTSTACK_"):
                values[key] = value
    return values


class AgentMail:
    """The MCP endpoint, spoken to exactly as the hooks speak to it."""

    def __init__(self, url: str, token: str = "") -> None:
        self.url = url
        self.token = token
        self._id = 0
        self._registrations: dict[str, str] = {}
        self._schema: dict[str, set] | None = None

    def remember_token(self, name: str, registration_token: str) -> None:
        if registration_token:
            self._registrations[name] = registration_token

    # Builds disagree about identity: one refuses fetch_inbox without a token,
    # another rejects that same argument, and they do not even agree on its
    # name. Rather than encode one server's habits, read the advertised schema
    # and shape each call to fit the server actually answering.
    TOKEN_PARAMS = ("registration_token", "sender_token", "agent_token")

    def parameters(self, tool: str) -> set:
        if self._schema is None:
            self._schema = {}
            try:
                listing = self._rpc("tools/list", {})
            except (OSError, ValueError, Fail):
                return set()
            for tool_def in (listing.get("tools") or []):
                params = ((tool_def.get("inputSchema") or {}).get("properties") or {})
                self._schema[tool_def.get("name", "")] = set(params)
        return self._schema.get(tool, set())

    def prepare(self, tool: str, arguments: dict, agent: str = "") -> dict:
        allowed = self.parameters(tool)
        if not allowed:
            return arguments
        prepared = {k: v for k, v in arguments.items() if k in allowed}
        token = self._registrations.get(agent) if agent else ""
        if token:
            for field in self.TOKEN_PARAMS:
                if field in allowed:
                    prepared[field] = token
                    break
        return prepared

    def _rpc(self, method: str, params: dict, timeout: float = 20.0):
        self._id += 1
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": f"selftest-{self._id}",
            "method": method,
            "params": params,
        }).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.url, data=payload, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
        message = json.loads(raw)
        if "error" in message:
            raise Fail(f"{method} failed: {message['error']}")
        return message.get("result") or {}

    def call(self, tool: str, arguments: dict, agent: str = "", timeout: float = 20.0):
        result = self._rpc(
            "tools/call",
            {"name": tool, "arguments": self.prepare(tool, arguments, agent)},
            timeout,
        )
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        for block in result.get("content") or []:
            if block.get("type") == "text":
                text = block.get("text") or ""
                try:
                    return json.loads(text)
                except ValueError:
                    # The server reports refusals as plain text inside a
                    # successful envelope. Treating that as data is how a
                    # diagnostic ends up blaming the wrong component.
                    lowered = text.lower()
                    if "error calling tool" in lowered or "validation error" in lowered:
                        raise Fail(f"{tool}: {text.strip()}")
                    return text
        return result


def read_token(env: dict[str, str]) -> str:
    mail_env = env.get("AGENTSTACK_MAIL_ENV", "")
    if not mail_env:
        return ""
    try:
        for line in pathlib.Path(mail_env).read_text(encoding="utf-8").splitlines():
            if line.startswith("HTTP_BEARER_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        return ""
    return ""


def verify_claude_mcp_registration(env: dict[str, str], report: Reporter) -> None:
    """Prove Claude Code can see the fixed tool namespace /delegate allows."""
    path = pathlib.Path(
        env.get("AGENTSTACK_CLAUDE_JSON")
        or os.environ.get("AGENTSTACK_CLAUDE_JSON", "")
        or pathlib.Path.home() / ".claude.json"
    ).expanduser()
    expected_url = env.get("AGENTSTACK_MCP_URL", "http://127.0.0.1:8765/mcp")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        entry = config.get("mcpServers", {}).get("mcp-agent-mail")
    except (AttributeError, OSError, ValueError) as exc:
        raise Fail(
            f"Claude MCP registration is missing or unreadable at {path}: {exc}; "
            "run agentstack-doctor for the safe registration command"
        ) from exc
    if not isinstance(entry, dict) or entry.get("type") != "http":
        raise Fail(
            f"Claude MCP entry 'mcp-agent-mail' is not an HTTP server in {path}; "
            "/delegate cannot see its allowed mcp__mcp-agent-mail__* tools"
        )
    if entry.get("url") != expected_url:
        raise Fail(
            f"Claude MCP entry 'mcp-agent-mail' points to {entry.get('url')!r}, "
            f"not the installed endpoint {expected_url!r}"
        )
    bearer = read_token(env)
    authorization = (entry.get("headers") or {}).get("Authorization")
    if bearer and authorization != f"Bearer {bearer}":
        raise Fail(
            "Claude MCP entry 'mcp-agent-mail' has missing or stale authorization; "
            "run agentstack-doctor for the safe registration command"
        )
    report.ok("Claude Code has the fixed mcp-agent-mail MCP registration")


def dashboard(url: str, path: str, timeout: float = 15.0):
    with urllib.request.urlopen(f"{url}{path}", timeout=timeout) as response:
        return json.load(response)


def register_pair(mail: AgentMail, project_key: str, report: Reporter) -> list[str]:
    """Let the server name them: name rules differ between builds."""
    names = []
    for role in ("selftest sender", "selftest recipient"):
        response = mail.call("register_agent", {
            "project_key": project_key,
            "program": "agentstack-selftest",
            "model": "none",
            "task_description": f"install self-test ({role}); retired automatically",
        })
        name = (response or {}).get("name") if isinstance(response, dict) else None
        if not name:
            raise Fail(f"register_agent returned no name: {response!r}")
        # A stock server requires this token on every later call for the agent;
        # a permissive one ignores it. Carry it either way, so the self-test
        # does not pass only on the machine it was written on.
        mail.remember_token(name, response.get("registration_token", ""))
        names.append(name)
    report.ok(f"registered two agents: {names[0]} and {names[1]}")
    return names


def introduce(mail: AgentMail, project_key: str, sender: str, recipient: str) -> None:
    """Some builds require an approved contact before the first message.

    The server says so in the refusal and names the two calls that fix it, so
    do what it asks instead of declaring the installation broken.
    """
    mail.call("request_contact", {
        "project_key": project_key,
        "from_agent": sender,
        "to_agent": recipient,
    }, agent=sender)
    mail.call("respond_contact", {
        "project_key": project_key,
        "to_agent": recipient,
        "from_agent": sender,
        "accept": True,
    }, agent=recipient)


def exchange(mail: AgentMail, project_key: str, pair: list[str], report: Reporter) -> None:
    sender, recipient = pair
    subject = "selftest ping"
    ping = {
        "project_key": project_key,
        "sender_name": sender,
        "to": [recipient],
        "subject": subject,
        "body_md": "This message exists only to prove delivery works.",
        "importance": "normal",
    }
    try:
        mail.call("send_message", ping, agent=sender)
    except Fail as refusal:
        if "contact" not in str(refusal).lower():
            raise
        introduce(mail, project_key, sender, recipient)
        mail.call("send_message", ping, agent=sender)
        report.note("the server required a contact handshake first; completed it")
    inbox = mail.call("fetch_inbox", {
        "project_key": project_key,
        "agent_name": recipient,
        "include_bodies": True,
        "limit": 5,
    }, agent=recipient)
    rows = inbox.get("result") if isinstance(inbox, dict) else inbox
    delivered = any(
        isinstance(row, dict) and row.get("subject") == subject
        for row in (rows or [])
    )
    if not delivered:
        raise Fail(
            f"{sender} sent a message but {recipient} cannot read it "
            "— delivery or the database is not working"
        )
    report.ok(f"{recipient} received the message {sender} sent")

    pong = {
        "project_key": project_key,
        "sender_name": recipient,
        "to": [sender],
        "subject": "selftest pong",
        "body_md": "And this one proves the return path.",
        "importance": "normal",
    }
    try:
        mail.call("send_message", pong, agent=recipient)
    except Fail as refusal:
        if "contact" not in str(refusal).lower():
            raise
        introduce(mail, project_key, recipient, sender)
        mail.call("send_message", pong, agent=recipient)
    report.ok("the reply travelled back the other way")


def reservations(mail: AgentMail, project_key: str, pair: list[str], report: Reporter) -> None:
    holder, rival = pair
    mail.call("file_reservation_paths", {
        "project_key": project_key,
        "agent_name": holder,
        "paths": [RESERVED_PATH],
        "ttl_seconds": 120,
        "exclusive": True,
        "reason": "install self-test",
    }, agent=holder)
    contested = mail.call("macro_file_reservation_cycle", {
        "project_key": project_key,
        "agent_name": rival,
        "paths": [RESERVED_PATH],
        "ttl_seconds": 60,
    }, agent=rival)
    text = json.dumps(contested)
    if holder not in text:
        report.fail(
            "a second agent claimed a path the first one holds",
            "file reservations are not protecting concurrent edits",
        )
    else:
        report.ok("a conflicting reservation was reported against the holder")
    mail.call("release_file_reservations", {
        "project_key": project_key,
        "agent_name": holder,
        "paths": [RESERVED_PATH],
    }, agent=holder)


def dashboard_sees(url: str, pair: list[str], report: Reporter) -> None:
    """Does the dashboard read the database agent-mail just wrote to?

    Ask the graph, not the deck. The deck (`/api/agents`) is built from tmux
    sessions and shows agents that have one; these two were registered over
    HTTP and have none, so the deck could never list them and this check used
    to fail on every clean run — while blaming the database, which was fine.
    A tester diagnosed that for us.

    It is the same mistake this whole exercise is about, made by the thing
    meant to catch it: the checker held a different model of the system than
    the system did. The graph is built from the database, so it answers the
    question that was actually being asked.
    """
    try:
        graph = dashboard(url, "/api/graph?all=1")
    except (OSError, ValueError) as exc:
        raise Fail(f"the dashboard API at {url} did not answer: {exc}")
    known = {node.get("name") for node in (graph.get("nodes") or [])}
    missing = [name for name in pair if name not in known]
    if missing:
        raise Fail(
            f"the dashboard graph does not contain {', '.join(missing)} — it is "
            "probably reading a different database than agent-mail writes to"
        )
    report.ok("the dashboard reads the same database agent-mail wrote to")

    edges = graph.get("edges") or []
    linked = any(
        {edge.get("source"), edge.get("target")} == set(pair) for edge in edges
    )
    if linked:
        report.ok("the dashboard draws the link between them")
    else:
        report.fail(
            "the dashboard lists both agents but draws no link between them",
            "messages exist but the graph is not reading them",
        )


def cleanup(mail: AgentMail, project_key: str, pair: list[str], report: Reporter) -> None:
    """Leave nothing behind: a test that litters the graph is a bug of its own."""
    stranded = []
    for name in pair:
        args = {"project_key": project_key, "agent_name": name}
        for attempt in (name, ""):          # with the owner token, then without
            try:
                mail.call("retire_agent", args, agent=attempt)
                break
            except (Fail, OSError, ValueError):
                continue
        else:
            stranded.append(name)
    if stranded:
        # Some builds bind agent ownership to the MCP session, so a one-shot
        # client cannot retire what it registered. That is a limitation here,
        # not a broken install — but say so plainly instead of claiming a
        # cleanup that did not happen.
        report.note(
            "left " + ", ".join(stranded) + " registered: this server only lets "
            "the owning session retire an agent. Retire them from the dashboard."
        )
    else:
        report.ok("retired both test agents")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check that an installed agent stack actually works.",
    )
    parser.add_argument(
        "--install-dir",
        default=os.environ.get("AGENTSTACK_HOME", str(pathlib.Path.home() / ".agentstack")),
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the test agents registered (for inspecting the dashboard)",
    )
    args = parser.parse_args()

    report = Reporter()
    pair: list[str] = []
    mail = None
    project_key = ""
    try:
        env = load_env(pathlib.Path(args.install_dir).expanduser())
        mcp_url = env.get("AGENTSTACK_MCP_URL", "http://127.0.0.1:8765/mcp")
        project_key = env.get("AGENTSTACK_PROJECT_KEY", "")
        dash_url = f"http://127.0.0.1:{env.get('AGENTSTACK_PORT', '8770')}"
        if not project_key:
            raise Fail("AGENTSTACK_PROJECT_KEY is not set in the installed env.sh")
        report.ok(f"read the installed environment from {args.install_dir}")
        verify_claude_mcp_registration(env, report)

        mail = AgentMail(mcp_url, read_token(env))
        health = mail.call("health_check", {})
        if not isinstance(health, dict) or health.get("status") not in ("ok", "healthy"):
            raise Fail(f"agent-mail at {mcp_url} is not healthy: {health!r}")
        report.ok(f"agent-mail answered at {mcp_url}")

        mail.call("ensure_project", {"human_key": project_key})
        pair = register_pair(mail, project_key, report)
        exchange(mail, project_key, pair, report)
        reservations(mail, project_key, pair, report)
        dashboard_sees(dash_url, pair, report)
    except Fail as exc:
        report.fail(str(exc))
    except (OSError, ValueError) as exc:
        report.fail(f"{type(exc).__name__}: {exc}")
    finally:
        if mail and pair and not args.keep:
            try:
                cleanup(mail, project_key, pair, report)
            except Exception as exc:  # cleanup must never mask the real result
                report.fail("cleanup failed", str(exc))
        elif pair and args.keep:
            report.note(f"left {', '.join(pair)} registered as requested")

    print()
    if report.failures:
        print(f"self-test failed: {len(report.failures)} problem(s)", file=sys.stderr)
        for failure in report.failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "\nPaste this output when reporting the problem; it identifies which "
            "part of the chain is broken.",
            file=sys.stderr,
        )
        return 1
    print("self-test passed: two agents registered, exchanged messages, "
          "held a reservation, and appeared on the dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
