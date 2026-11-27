"""One-launch receipt for a delegated child's first ORRERY Mail tool call."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentstack_codex_app.agent_mail_client import AgentMailClient, AgentMailError, HttpJsonRpcTransport

SUBJECT = "ORRERY child startup check"
PREFIX = ".mail-start-"
NONCE = re.compile(r"[0-9a-f]{32}")


def begin(runtime: Path, agent: str, project: str, program: str) -> Path:
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    attempt = runtime / (PREFIX + secrets.token_hex(16))
    attempt.mkdir(mode=0o700)
    try:
        (attempt / "binding.json").write_text(json.dumps([agent, project, program]), encoding="utf-8")
        for outcome in ("ready", "failed"):
            (attempt / outcome).write_text(outcome + "\n", encoding="ascii")
    except OSError:
        shutil.rmtree(attempt, ignore_errors=True)
        raise
    return attempt


def confirm(runtime: Path, nonce: str, binding: dict, recipients: list[str]) -> dict:
    # Use a directory handle so a late reply cannot recreate an expired
    # attempt. A previous session has a different nonce even on name reuse.
    agent = binding["agent_name"]
    if not NONCE.fullmatch(nonce) or recipients != [agent]:
        raise ValueError("invalid ORRERY Mail startup check")
    directory = os.open(runtime / (PREFIX + nonce), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with os.fdopen(os.open("binding.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)) as handle:
            expected = json.load(handle)
        if expected != [agent, binding["project_key"], binding["program"]]:
            raise ValueError("the ORRERY Mail startup check belongs to a different child")
        if settle(directory, "ready") != "ready":
            raise ValueError("the ORRERY Mail startup check has already failed")
    finally:
        os.close(directory)
    return {"startup_confirmed": True}


def settle(directory: int, outcome: str) -> str:
    # Publishing and rejecting a receipt compete for the same atomic link.
    # A delayed native call must never succeed after the launcher rejects it.
    try:
        os.link(outcome, "verdict", src_dir_fd=directory, dst_dir_fd=directory)
    except FileExistsError:
        with os.fdopen(os.open("verdict", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)) as handle:
            return handle.read().strip()
    return outcome


def notify(token_file: Path, endpoint: str, mail_env: Path, bearer_mode: str,
           project: str, agent: str, parent: str, reason: str) -> None:
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("child registration token is unavailable")
    bearer = None
    if bearer_mode != "disabled":
        bearer = os.environ.get("MCP_AGENT_MAIL_TOKEN", "").strip() or None
        if not bearer and mail_env.is_file():
            for line in mail_env.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip().removeprefix("export ").strip() == "HTTP_BEARER_TOKEN":
                    bearer = value.strip().strip("\"'") or None
                    break
        if bearer_mode == "enabled" and not bearer:
            raise ValueError("HTTP bearer token is unavailable")
    client = AgentMailClient(HttpJsonRpcTransport(endpoint, bearer_token=bearer, timeout=4))
    client.send_message(
        project_key=project, agent_name=agent, registration_token=token, to=[parent],
        subject=f"{agent} could not start", body_md=f"The ORRERY launcher stopped the child.\n\n{reason}",
        importance="high", cc=None, bcc=None, ack_required=False, thread_id=None, topic=None,
    )


def main() -> int:
    action, *args = sys.argv[1:]
    try:
        if action == "begin":
            runtime, *binding = args
            print(begin(Path(runtime), *binding))
        elif action == "reject":
            directory = os.open(args[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                return 0 if settle(directory, "failed") == "failed" else 1
            finally:
                os.close(directory)
        elif action == "notify":
            token_file, endpoint, mail_env, *notice = args
            notify(Path(token_file), endpoint, Path(mail_env), *notice)
        else:
            return 2
    except (OSError, ValueError, AgentMailError):
        # The calling launcher prints its fixed failure reason. Never put a
        # credential or an upstream error body into the shared spawn log.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
