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

    def call(self, tool: str, arguments: dict, timeout: float = 20.0):
        self._id += 1
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": f"selftest-{self._id}",
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
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
            raise Fail(f"{tool} failed: {message['error']}")
        result = message.get("result") or {}
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        for block in result.get("content") or []:
            if block.get("type") == "text":
                text = block.get("text") or ""
                try:
                    return json.loads(text)
                except ValueError:
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


def dashboard(url: str, path: str, timeout: float = 15.0):
    with urllib.request.urlopen(f"{url}{path}", timeout=timeout) as response:
        return json.load(response)


def names_of(payload) -> set[str]:
    if isinstance(payload, dict):
        for key in ("agents", "nodes"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return {r.get("name") for r in rows if isinstance(r, dict)}
    return set()


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
        names.append(name)
    report.ok(f"registered two agents: {names[0]} and {names[1]}")
    return names


def exchange(mail: AgentMail, project_key: str, pair: list[str], report: Reporter) -> None:
    sender, recipient = pair
    subject = "selftest ping"
    mail.call("send_message", {
        "project_key": project_key,
        "sender_name": sender,
        "to": [recipient],
        "subject": subject,
        "body_md": "This message exists only to prove delivery works.",
        "importance": "normal",
    })
    inbox = mail.call("fetch_inbox", {
        "project_key": project_key,
        "agent_name": recipient,
        "include_bodies": True,
        "limit": 5,
    })
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

    mail.call("send_message", {
        "project_key": project_key,
        "sender_name": recipient,
        "to": [sender],
        "subject": "selftest pong",
        "body_md": "And this one proves the return path.",
        "importance": "normal",
    })
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
    })
    contested = mail.call("macro_file_reservation_cycle", {
        "project_key": project_key,
        "agent_name": rival,
        "paths": [RESERVED_PATH],
        "ttl_seconds": 60,
    })
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
    })


def dashboard_sees(url: str, pair: list[str], report: Reporter) -> None:
    try:
        agents = dashboard(url, "/api/agents")
    except (OSError, ValueError) as exc:
        raise Fail(f"the dashboard API at {url} did not answer: {exc}")
    missing = [name for name in pair if name not in names_of(agents)]
    if missing:
        raise Fail(
            f"the dashboard does not list {', '.join(missing)} — it is probably "
            "reading a different database than agent-mail writes to"
        )
    report.ok("the dashboard lists both agents")

    graph = dashboard(url, "/api/graph?all=1")
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
    for name in pair:
        try:
            mail.call("retire_agent", {
                "project_key": project_key,
                "agent_name": name,
                "reason": "install self-test finished",
            })
        except (Fail, OSError, ValueError) as exc:
            report.fail(f"could not retire {name}", str(exc))
            return
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
