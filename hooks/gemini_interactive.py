"""Launch and observe a macOS Antigravity child using the common retained state."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import uuid

if "child_resume" not in globals():
    if __package__:
        from . import child_resume
    else:
        import child_resume


PROGRAM = "antigravity"
TMUX_FORMAT = "#{session_name}|#{session_id}|#{session_created}|#{pid}"
WRITE_TOOLS = {"write_to_file": "Write", "replace_file_content": "Edit",
               "multi_replace_file_content": "Edit"}
ACTIVITY = {"idle": "wait", "working": "work", "tool_use": "work",
            "reviewing": "ask"}
MAIL_TOOLS = ("send_message", "fetch_inbox", "acknowledge_message", "reserve_files",
              "release_reservations", "renew_reservations", "show_files")


def _home():
    return Path(__file__).resolve().parents[1]


def history_helper():
    return _home() / "dashboard" / "agy_history.py"


def _history():
    path = history_helper()
    # The optional Reader also runs as a CLI, so its sibling imports resolve
    # relative to dashboard/ when the launcher loads it in-process.
    sys.path.insert(0, str(path.parent))
    try:
        return runpy.run_path(str(path))
    finally:
        sys.path.pop(0)


def saved_conversation(runtime, name, registration):
    history = _history()
    binding = history["binding"](runtime, name, registration, None)
    return binding.get("history_session_id") if binding.get("history_binding") == "bound" else None


def _status_path(runtime, name):
    return child_resume._paths(runtime, name)[0].with_suffix(".agy-status.json")


def read_status(runtime, name, identity):
    try:
        record = child_resume._load_state(_status_path(runtime, name))
    except (OSError, ValueError, child_resume.ResumeStateError):
        return {}
    return record if record.get("tmux") == identity else {}


def _configured_hooks(python):
    command = shlex.join([python, str(Path(__file__).resolve()), "guard"])
    history = _history()
    reader = {"type": "command", "command": shlex.join([python, str(history_helper()), "hook"]),
              "timeout": 2}
    return {
        history["GROUP"]: {event: [reader] for event in history["HOOKS"]},
        "orrery-child": {"PreToolUse": [{
            "matcher": "view_file|write_to_file|replace_file_content|multi_replace_file_content|run_command",
            "hooks": [{"type": "command", "command": command, "timeout": 12}],
        }]},
    }


def _guard_config(cwd):
    path = cwd / ".agents" / "hooks.json"
    if path.parent.is_symlink():
        raise ValueError("Antigravity workspace configuration cannot be a symlink")
    return path


def validate_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("Antigravity child launch settings are missing")
    cwd = Path(plan["cwd"])
    info = cwd.stat()
    if (not cwd.is_absolute() or cwd.resolve() != cwd or not cwd.is_dir()
            or [info.st_dev, info.st_ino] != plan["workspace_file_id"]):
        raise ValueError("Antigravity child worktree was replaced or moved")
    path = _guard_config(cwd)
    if (path.is_symlink() or child_resume._load_state(path) != _configured_hooks(plan["python"])):
        raise ValueError("Antigravity child reservation hook is unavailable")
    if not all(os.path.isfile(plan[field]) and os.access(plan[field], os.X_OK)
               for field in ("binary", "python")):
        raise ValueError("The saved Antigravity CLI is unavailable")
    return cwd


def prepare(runtime, name, project, parent, *, source, cwd, binary, model, effort):
    cwd = cwd.resolve(strict=True)
    binary = shutil.which(binary)
    if not binary or not history_helper().is_file():
        raise ValueError("Antigravity CLI and Reader are required for a managed interactive child")
    path = _guard_config(cwd)
    if path.exists() or path.is_symlink():
        raise ValueError("The worktree already has .agents/hooks.json")
    info = cwd.stat()
    plan = {"cwd": str(cwd), "workspace_file_id": [info.st_dev, info.st_ino],
            "binary": os.path.abspath(binary), "python": sys.executable, "model": model, "effort": effort}
    hooks = _configured_hooks(plan["python"])
    generation = uuid.uuid4().hex
    child_resume.stage_registration(runtime, name, project_key=project, program=PROGRAM,
                                    generation=generation, source=source,
                                    binding=Path(str(source) + ".binding.json"))
    try:
        child_resume.prepare_active_state(runtime, name, project_key=project, program=PROGRAM,
                                          parent_agent=parent, generation=generation,
                                          gemini_launch=plan)
        child_resume._atomic_json(path, hooks)
        child_resume.finish_registration(runtime, name, generation=generation, rollback=False)
    except Exception:
        child_resume.finish_registration(runtime, name, generation=generation, rollback=True)
        path.unlink(missing_ok=True)
        raise


def command(plan, cwd, *, prompt=None, conversation=None):
    if (prompt is None) == (conversation is None):
        raise ValueError("Provide the first task or an existing conversation")
    settings = {"trustedWorkspaces": [str(cwd)], "enableTerminalSandbox": True,
                "statusLine": {
                    "type": "command",
                    "command": shlex.join([plan["python"], str(Path(__file__).resolve()), "status"]),
                }}
    try:
        configured = json.loads((Path.home() / ".gemini/antigravity-cli/settings.json").read_text())
    except FileNotFoundError:
        configured = {}
    permissions = configured.get("permissions") or {}
    settings["permissions"] = {**permissions, "allow": list(dict.fromkeys(
        [*permissions.get("allow", ()), *(f"mcp(orrery-mail/{tool})" for tool in MAIL_TOOLS)]
    ))}
    return [plan["binary"], "--mode", "auto", "--sandbox", "--settings",
            json.dumps(settings, separators=(",", ":")), "--model", plan["model"],
            "--effort", plan["effort"],
            *(["--conversation", conversation] if conversation else ["-i", prompt])]


def run(runtime, name, task_file=None, conversation=None):
    path, token, _child_home, _mcp, _lock = child_resume._paths(runtime, name)
    state = child_resume._load_state(path)
    project = os.environ["AGENTSTACK_PROJECT_KEY"]
    child_resume._validate_identity(state, agent_name=name, project_key=project, program=PROGRAM)
    canonical = child_resume._read_private(token, "canonical credential", child_resume.MAX_TOKEN_BYTES)
    if canonical.decode().strip() != state["registration_token"]:
        raise ValueError("Antigravity child identity changed")
    plan = state["gemini_launch"]
    cwd = validate_plan(plan)
    identity = subprocess.check_output(
        ["tmux", "display-message", "-p", "-t", os.environ["TMUX_PANE"], TMUX_FORMAT],
        text=True, timeout=5).strip()
    if identity.split("|", 1)[0] != name:
        raise ValueError("Antigravity child is in a different tmux session")

    prompt = None
    if task_file:
        event = json.loads(child_resume._read_private(task_file, "first task", 1024 * 1024))
        prompt = event["message"]["content"]
        task_file.unlink()
    if conversation is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", conversation):
            raise ValueError("The saved Antigravity conversation ID is invalid")
        if not state.get("resume_in_progress_at"):
            raise ValueError("Antigravity resume was not prepared")

    env = dict(os.environ, AGENT_NAME=name, AGENTSTACK_RESERVED_IDENTITY="1",
               PARENT_AGENT=state["parent_agent"], AGENTSTACK_GEMINI_TMUX_IDENTITY=identity)
    if conversation:
        count = env.get("GIT_CONFIG_COUNT", "0")
        index = int(count) if count.isdecimal() else 0
        env[f"GIT_CONFIG_KEY_{index}"] = "core.excludesFile"
        env[f"GIT_CONFIG_VALUE_{index}"] = str(runtime / f"gemini-git-excludes-{name}")
        env["GIT_CONFIG_COUNT"] = str(index + 1)

    os.chdir(cwd)
    status_path = _status_path(runtime, name)
    status_path.unlink(missing_ok=True)
    try:
        if conversation:
            child_resume.prepare_active_state(runtime, name, project_key=project, program=PROGRAM)
        env["AGENTSTACK_AGY_HISTORY_LAUNCH"] = subprocess.check_output(
            [sys.executable, str(history_helper()), "prepare", str(state["agent_id"]), identity],
            env=env, text=True, timeout=5).strip()
        return subprocess.run(command(plan, cwd, prompt=prompt, conversation=conversation),
                              env=env).returncode
    finally:
        status_path.unlink(missing_ok=True)
        subprocess.run(["/bin/bash", str(_home() / "hooks" / "cleanup-child-agent.sh"), name],
                       env=env, check=False)


def status(runtime, name, payload):
    if not os.environ.get("AGENTSTACK_AGY_HISTORY_LAUNCH"):
        return
    model = payload.get("model") or {}
    label = model.get("display_name") or model.get("id") or ""
    effort = model.get("effort") or ""
    mode = payload.get("cycle_mode") or ""
    details = [value for value in (mode, label, effort)
               if isinstance(value, str) and value and all(c.isprintable() for c in value)]
    identity = os.environ["AGENTSTACK_GEMINI_TMUX_IDENTITY"]
    old = read_status(runtime, name, identity)
    record = {"tmux": identity,
              "activity": ACTIVITY.get(payload.get("agent_state")),
              "model_id": str(model.get("id") or "")[:128], "effort": str(effort)[:20],
              "conversation": old.get("conversation")}
    sid, transcript = payload.get("conversation_id"), payload.get("transcript_path")
    if sid and sid != old.get("conversation") and transcript and Path(transcript).is_file():
        # A resumed conversation can paint its status before it invokes the model.
        # The Reader validates the same native ID, path, workspace and launch nonce.
        binding = {"conversationId": payload["conversation_id"],
                   "transcriptPath": payload["transcript_path"],
                   "workspacePaths": [payload.get("cwd")]}
        saved = subprocess.run([sys.executable, str(history_helper()), "hook"],
                       input=json.dumps(binding), text=True, stdout=subprocess.DEVNULL,
                       timeout=2, check=False)
        if saved.returncode == 0:
            record["conversation"] = sid
    if record != old:
        child_resume._atomic_json(_status_path(runtime, name), record)
    print(" · ".join(details))


def guard(event, cwd):
    tool = event["toolCall"]
    name, args = tool["name"], tool["args"]
    inputs = {}
    overwrite = None
    if name in WRITE_TOOLS:
        target = Path(args["TargetFile"])
        target = (cwd / target).resolve()
        if (args.get("IsArtifact") or not target.is_relative_to(cwd)
                or any(part.casefold() == ".git" for part in target.relative_to(cwd).parts)
                or target in {_guard_config(cwd), cwd / ".agents" / "mcp_config.json"}):
            return {"decision": "deny", "reason": "Edit files only in the child worktree"}
        inputs["file_path"] = str(target)
        mode = WRITE_TOOLS[name]
    elif name == "run_command":
        workdir = (cwd / Path(args.get("Cwd") or cwd)).resolve()
        if args.get("BypassSandbox") or not workdir.is_relative_to(cwd):
            return {"decision": "deny", "reason": "Use a sandboxed command inside the child worktree"}
        inputs["command"] = args["CommandLine"]
        cwd = workdir
        overwrite = {"Cwd": str(cwd)}
        mode = "Bash"
    else:
        mode = "Read"
        inputs["file_path"] = str((cwd / Path(args["AbsolutePath"])).resolve())
    payload = json.dumps({"tool_name": mode, "tool_input": inputs,
                          "cwd": str(cwd), "session_id": event.get("conversationId", "")})
    checks = ["deny-llm-key-read.py"]
    if mode != "Read":
        checks.append("check-agent-registered.sh")
    if name in WRITE_TOOLS:
        checks.append("check-file-reservation.sh")
    for hook in checks:
        interpreter = sys.executable if hook.endswith(".py") else "/bin/bash"
        result = subprocess.run([interpreter, str(_home() / "hooks" / hook)],
                                input=payload, text=True, capture_output=True, cwd=cwd, timeout=3)
        if result.returncode:
            return {"decision": "deny", "reason": f"ORRERY blocked this action ({hook})"}
    return {"decision": "allow", **({"overwrite": overwrite} if overwrite else {})}


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="action", required=True)
    setup = actions.add_parser("prepare")
    setup.add_argument("source", type=Path)
    setup.add_argument("cwd", type=Path)
    setup.add_argument("binary")
    setup.add_argument("model")
    setup.add_argument("effort")
    launch = actions.add_parser("run")
    group = launch.add_mutually_exclusive_group(required=True)
    group.add_argument("--task-file", type=Path)
    group.add_argument("--conversation")
    actions.add_parser("status")
    actions.add_parser("guard")
    for action in ("ready", "wake-ready"):
        ready = actions.add_parser(action)
        ready.add_argument("name")
        ready.add_argument("identity")
    args = parser.parse_args()
    runtime = Path(os.environ["AGENTSTACK_RUNTIME_DIR"])
    if args.action in {"ready", "wake-ready"}:
        activity = read_status(runtime, args.name, args.identity).get("activity")
        return 0 if activity == "wait" or (args.action == "ready" and activity) else 1
    name = os.environ["AGENT_NAME"]
    if args.action == "prepare":
        prepare(runtime, name, os.environ["AGENTSTACK_PROJECT_KEY"], os.environ["PARENT_AGENT"],
                source=args.source, cwd=args.cwd, binary=args.binary, model=args.model, effort=args.effort)
    elif args.action == "run":
        return run(runtime, name, args.task_file, args.conversation)
    elif args.action == "status":
        status(runtime, name, json.loads(sys.stdin.buffer.read(256 * 1024)))
    else:
        try:
            state = child_resume._load_state(child_resume._paths(runtime, name)[0])
            child_resume._validate_identity(state, agent_name=name, program=PROGRAM,
                                            project_key=os.environ["AGENTSTACK_PROJECT_KEY"])
            decision = guard(json.loads(sys.stdin.buffer.read(1024 * 1024)),
                             Path(state["gemini_launch"]["cwd"]))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
            decision = {"decision": "deny", "reason": "ORRERY could not check the child worktree"}
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(f"Antigravity child: {error}")
