"""Launch and observe a macOS Antigravity child using the common retained state."""

from __future__ import annotations

import argparse
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

if "child_resume" not in globals():
    if __package__:
        from . import child_resume
    else:
        import child_resume
if "child_tools" not in globals():
    if __package__:
        from . import child_tools
    else:
        import child_tools


PROGRAM = "antigravity"
TMUX_FORMAT = "#{session_name}|#{session_id}|#{session_created}|#{pid}"
MCP_PREPARING = "@orrery-agy-mcp-preparing"
STARTUP_TIMEOUT = 22
TOOL_REFUSAL_RULE = Path(".agents/rules/orrery-tool-refusals.md")
RULE_HEADER = b"---\ntrigger: always_on\n---\n\n<!-- ORRERY: tool refusal rule -->\n\n"
WRITE_TOOLS = {"write_to_file": "Write", "replace_file_content": "Edit",
               "multi_replace_file_content": "Edit"}
ACTIVITY = {"idle": "wait", "working": "work", "tool_use": "work",
            "reviewing": "ask"}
# Keep in step with the session-bound proxy exposed to Codex children.
MAIL_TOOLS = ("bootstrap", "fetch_inbox", "send_message", "acknowledge_message",
              "reserve_files", "renew_reservations", "release_reservations",
              "runtime_status", "show_files", "whois")


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


def _startup_path(runtime, name):
    return _status_path(runtime, name).with_suffix(".startup")


def _startup_state(runtime, name, identity):
    try:
        state = child_resume._load_state(_startup_path(runtime, name))
    except (OSError, ValueError, child_resume.ResumeStateError):
        return {}
    return state if state.get("tmux") == identity else {}


def read_status(runtime, name, identity):
    try:
        record = child_resume._load_state(_status_path(runtime, name))
    except (OSError, ValueError, child_resume.ResumeStateError):
        return {}
    return record if record.get("tmux") == identity else {}


def _configured_hooks(python, *, suffix=""):
    command = shlex.join([python, str(Path(__file__).resolve()), "guard"])
    history = _history()
    reader = {"type": "command", "command": shlex.join([python, str(history_helper()), "hook"]),
              "timeout": 2}
    return {
        history["GROUP"] + suffix: {event: [reader] for event in history["HOOKS"]},
        "orrery-child" + suffix: {"PreToolUse": [{
            "matcher": "view_file|write_to_file|replace_file_content|multi_replace_file_content|run_command",
            "hooks": [{"type": "command", "command": command, "timeout": 12}],
        }]},
    }


def _guard_config(cwd):
    path = cwd / ".agents" / "hooks.json"
    if path.parent.is_symlink():
        raise ValueError("Antigravity workspace configuration cannot be a symlink")
    return path


def write_headless_consent_hook(cwd):
    if sys.platform != "darwin":
        return
    path = _guard_config(cwd)
    if path.exists() or path.is_symlink():
        raise ValueError("The worktree already has .agents/hooks.json")
    script = Path(__file__).resolve().with_name("macos_consent_guard.py")
    declaration = runpy.run_path(str(script))["antigravity_settings"]
    child_resume._atomic_json(path, declaration(sys.executable, script))


def write_tool_refusal_rule(cwd):
    _guard_config(cwd)
    path = cwd / TOOL_REFUSAL_RULE
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Antigravity tool refusal rule cannot be a symlink")
    if path.exists():
        old = child_resume._read_private(path, "tool refusal rule", 64 * 1024)
        if not old.startswith(RULE_HEADER):
            raise ValueError("The worktree already has a tool refusal rule at the ORRERY path")
    renderer = runpy.run_path(str(_home() / "bin/lib/instruction_blocks.py"))
    child_resume._atomic_bytes(path, RULE_HEADER + renderer["tool_refusal_block"]())


def validate_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("Antigravity child launch settings are missing")
    cwd = Path(plan["cwd"])
    info = cwd.stat()
    if (not cwd.is_absolute() or cwd.resolve() != cwd or not cwd.is_dir()
            or [info.st_dev, info.st_ino] != plan["workspace_file_id"]):
        raise ValueError("Antigravity child worktree was replaced or moved")
    if not plan.get("standalone"):
        path = _guard_config(cwd)
        if (path.is_symlink() or child_resume._load_state(path) != _configured_hooks(plan["python"])):
            raise ValueError("Antigravity child reservation hook is unavailable")
    if not all(os.path.isfile(plan[field]) and os.access(plan[field], os.X_OK)
               for field in ("binary", "python")):
        raise ValueError("The saved Antigravity CLI is unavailable")
    return cwd


def launch_parent(state):
    parent = state.get("parent_agent")
    if state["gemini_launch"].get("standalone") is True and parent is None:
        return ""
    if (not state["gemini_launch"].get("standalone") and isinstance(parent, str)
            and child_resume.PROXY_AGENT_NAME.fullmatch(parent)):
        return parent
    raise ValueError("Antigravity launch parent does not match its saved settings")


def prepare(runtime, name, project, parent, *, source, cwd, binary, model, effort,
            standalone=False, tools=()):
    cwd = cwd.resolve(strict=True)
    binary = shutil.which(binary)
    if not binary or not history_helper().is_file():
        raise ValueError("Antigravity CLI and Reader are required for a managed interactive child")
    if standalone == bool(parent):
        raise ValueError("Antigravity launch requires exactly one of standalone or a parent")
    path = None if standalone else _guard_config(cwd)
    if path is not None and (path.exists() or path.is_symlink()):
        raise ValueError("The worktree already has .agents/hooks.json")
    info = cwd.stat()
    plan = {"cwd": str(cwd), "workspace_file_id": [info.st_dev, info.st_ino],
            "binary": os.path.abspath(binary), "python": sys.executable,
            "model": model, "effort": effort, "tools": list(tools)}
    if standalone:
        plan["standalone"] = True
        validate_standalone_config(plan)
    else:
        hooks = _configured_hooks(plan["python"])
    generation = uuid.uuid4().hex
    child_resume.stage_registration(runtime, name, project_key=project, program=PROGRAM,
                                    generation=generation, source=source,
                                    binding=Path(str(source) + ".binding.json"))
    try:
        child_resume.prepare_active_state(runtime, name, project_key=project, program=PROGRAM,
                                          parent_agent=parent, generation=generation,
                                          gemini_launch=plan)
        if path is not None:
            child_resume._atomic_json(path, hooks)
        child_resume.finish_registration(runtime, name, generation=generation, rollback=False)
    except Exception:
        child_resume.finish_registration(runtime, name, generation=generation, rollback=True)
        if path is not None:
            path.unlink(missing_ok=True)
        raise


def _tool_selection(plan):
    return child_tools.normalize(tools=child_tools.parse_cli(plan.get("tools", [])))


def permission_selection(runtime, state, conversation):
    plan = state["gemini_launch"]
    return child_resume.permission_selection(runtime, state, conversation,
        workspace=plan["cwd"], workspace_file_id=plan["workspace_file_id"])


def validate_standalone_config(plan):
    child_tools.agy_validate_selection(_tool_selection(plan), cwd=plan["cwd"])


def _configure_standalone(directory, plan, mcp):
    directory.mkdir(mode=0o700)
    try:
        child_resume._atomic_json(directory / ".agents" / "mcp_config.json", mcp)
        hooks = _configured_hooks(plan["python"], suffix=f"-{uuid.uuid4().hex}")
        child_resume._atomic_json(directory / ".agents" / "hooks.json", hooks)
    except BaseException:
        shutil.rmtree(directory)
        raise


def _delegated_mcp_path(cwd):
    path = _guard_config(cwd).with_name("mcp_config.json")
    if path.exists() or path.is_symlink():
        raise ValueError("The worktree already has .agents/mcp_config.json")
    return path


def _mail_server():
    return {"command": str(_home() / "bin" / "agentstack-gemini-mcp"), "args": []}


def _workspace_mcp_snapshot(runtime, state, cwd):
    """Only replace the configuration written by this managed workspace."""
    path = _guard_config(cwd).with_name("mcp_config.json")
    raw = child_resume._read_private(path, "Antigravity workspace MCP", child_resume.MAX_STATE_BYTES)
    plan = state["gemini_launch"]
    hashes = plan.get("workspace_mcp_hashes")
    changed = "Antigravity's workspace MCP configuration changed outside its launcher"
    if hashes is not None:
        if isinstance(hashes, list) and hashlib.sha256(raw).hexdigest() in hashes:
            return path, raw
        raise ValueError(changed)
    # Earlier launches recorded the relay generation and used a fixed Mail
    # / Chrome configuration. Accept only those managed server definitions.
    legacy = json.loads(raw)
    servers = legacy.get("mcpServers") if isinstance(legacy, dict) else None
    if isinstance(servers, dict) and set(legacy) == {"mcpServers"}:
        expected = {"orrery-mail": _mail_server()}
        mode = _tool_selection(plan)["tools"].get("browser_mode")
        if mode in ("temporary", "personal"):
            name, definition = child_tools._browser_mcp(mode)
            expected[name] = definition
        screen = servers.get(child_tools.SCREEN_SERVER)
        generation = plan.get("screen_generation")
        if generation and isinstance(screen, dict):
            policy = child_tools._screen_state()
            target = dict(project=state["project_key"], runtime=policy["launch_runtime"](runtime),
                          agent=state["agent_name"], generation=generation)
            binding = policy["binding_path"](policy["Store"](runtime), target)
            if (screen.get("args") == ["-I", "-m", "agentstack_mail.screen_mcp"]
                    and screen.get("enabledTools") == list(policy["TOOLS"])
                    and isinstance(screen.get("env"), dict)
                    and screen["env"].get("AGENTSTACK_SCREEN_BINDING_FILE") == str(binding)
                    and set(screen) == {"args", "enabledTools", "command", "env"}
                    and isinstance(screen.get("command"), str) and Path(screen["command"]).is_absolute()):
                expected[child_tools.SCREEN_SERVER] = screen
        if servers == expected:
            return path, raw
    raise ValueError(changed)


def validate_permission_selection(runtime, state, spec):
    plan = state["gemini_launch"]
    cwd = validate_plan(plan)
    child_tools.agy_validate_selection(spec, cwd=cwd if plan.get("standalone") else None)
    mode = spec["tools"].get("browser_mode")
    if mode in ("temporary", "personal"):
        child_tools._browser_mcp(mode)
    if not plan.get("standalone"):
        _workspace_mcp_snapshot(runtime, state, cwd)
    child_tools.check_screen_relay(spec)


def _eager_tools(definition, names):
    return {**definition, "tools": {tool: {"eager": True} for tool in names}}


def _browser_tool_names(selection, name, definition):
    tools, _ = child_tools._approval(selection, name, definition, browser_server=name)
    return tools


def _eager_managed_servers(config, selection, screen_mode):
    """Register tools we own up front, keeping provider permission rules separate."""
    servers = config["mcpServers"]
    servers["orrery-mail"] = _eager_tools(servers["orrery-mail"], MAIL_TOOLS)
    if chrome := servers.get("chrome-devtools"):
        names = _browser_tool_names(selection, "chrome-devtools", chrome)
        if names:
            servers["chrome-devtools"] = _eager_tools(chrome, names)
    if screen_mode in ("view", "control"):
        policy = child_tools._screen_state()
        names = policy["VIEW_TOOLS"] if screen_mode == "view" else policy["TOOLS"]
        servers[child_tools.SCREEN_SERVER] = _eager_tools(servers[child_tools.SCREEN_SERVER], names)


def _matches_managed_mcp(home, cwd, config_dir, name, definition, eager_names):
    """Check that no inherited server can shadow an approved managed server."""
    configs = [("global", home / ".gemini" / "config" / "mcp_config.json"),
               ("workspace", Path(cwd) / ".agents" / "mcp_config.json")]
    if config_dir is not None:
        configs.append(("launch", config_dir / ".agents" / "mcp_config.json"))
    found = False
    for scope, path in configs:
        try:
            servers = child_tools._agy_inherited_servers(path, scope)
        except (OSError, child_tools.ToolsError):
            return False
        if name not in servers:
            continue
        entry = servers[name]
        if isinstance(entry, dict) and entry.get("disabled") is True:
            continue
        # A same-named inherited server can take precedence in Antigravity.
        if entry not in (definition, _eager_tools(definition, eager_names)):
            return False
        found = True
    return found


def _managed_browser_approval_rules(home, cwd, config_dir, selection):
    mode = selection["tools"].get("browser_mode")
    if mode not in ("temporary", "personal"):
        return []
    name, definition = child_tools._browser_mcp(mode)
    tools = _browser_tool_names(selection, name, definition)
    if not _matches_managed_mcp(home, cwd, config_dir, name, definition, tools):
        return []
    return [f"mcp({name}/{tool})" for tool in tools]


def _prompt_with_mcp_tools(prompt, *, managed_mail, parent, browser_mode):
    instructions = []
    if managed_mail:
        recipient = parent or "<recipient agent name>"
        arguments = json.dumps({"to": [recipient], "subject": "<subject>", "body_md": "<message>"})
        instructions.append(
            "For ORRERY Mail, use the registered `orrery-mail` MCP tool `send_message` "
            f"with arguments {arguments}. "
            "The bound Mail server supplies your identity. Antigravity's built-in `send_message` "
            "is for its own subagents."
        )
    if browser_mode in ("temporary", "personal"):
        instructions.append(
            "For browser work requested in the task, use the registered `chrome-devtools` MCP tools. "
            'To open a page, use `new_page` with arguments {"url": "<requested URL>"}.'
        )
    return "\n\n".join([*instructions, prompt])


def terminal_settings(home, sandbox):
    try:
        configured = json.loads((home / ".gemini/antigravity-cli/settings.json").read_text())
    except FileNotFoundError:
        configured = {}
    permissions = configured.get("permissions") or {}
    if not sandbox:
        # Auto requires the terminal sandbox. Allow shell commands in
        # accept-edits mode while preserving higher-priority deny and ask rules.
        allow = list(dict.fromkeys([*permissions.get("allow", ()), "command(*)"]))
        permissions = {**permissions, "allow": allow}
    return {"enableTerminalSandbox": sandbox, "permissions": permissions}


def terminal_mode(sandbox):
    return ["--mode", "auto", "--sandbox"] if sandbox else ["--mode", "accept-edits"]


def command(plan, cwd, *, prompt=None, conversation=None, config_dir=None, screen_mode=None, parent=None,
            selection=None):
    if (prompt is None) == (conversation is None):
        raise ValueError("Provide the first task or an existing conversation")
    sandbox = plan.get("standalone") is True or os.environ.get("AGENTSTACK_CHILD_SANDBOX") == "on"
    home = Path.home()
    settings = terminal_settings(home, sandbox)
    settings["trustedWorkspaces"] = [str(cwd)]
    settings["statusLine"] = {
        "type": "command",
        "command": shlex.join([plan["python"], str(Path(__file__).resolve()), "status"]),
    }
    permissions = settings["permissions"]
    if selection is None:
        selection = _tool_selection(plan)
    if screen_mode is None:
        screen_mode = child_tools.screen_mode(selection)
    screen_permissions = []
    if screen_mode in ("view", "control"):
        screen_policy = child_tools._screen_state()
        screen_tools = screen_policy["VIEW_TOOLS"] if screen_mode == "view" else screen_policy["TOOLS"]
        screen_permissions = [f"mcp({child_tools.SCREEN_SERVER}/{tool})" for tool in screen_tools]
    mail_rules = ([f"mcp(orrery-mail/{tool})" for tool in MAIL_TOOLS]
                  if _matches_managed_mcp(home, cwd, config_dir, "orrery-mail", _mail_server(), MAIL_TOOLS) else [])
    explicit = set(permissions.get("deny", ())) | set(permissions.get("ask", ()))
    browser_rules = [rule for rule in _managed_browser_approval_rules(home, cwd, config_dir, selection)
                     if rule not in explicit]
    settings["permissions"] = {**permissions, "allow": list(dict.fromkeys(
        [*permissions.get("allow", ()), *mail_rules, *screen_permissions, *browser_rules]
    ))}
    if prompt is not None:
        prompt = _prompt_with_mcp_tools(prompt, managed_mail=bool(mail_rules), parent=parent,
                                        browser_mode=selection["tools"].get("browser_mode"))
    extra = []
    if config_dir is not None:
        settings["trustedWorkspaces"].append(str(config_dir))
        extra = ["--add-dir", str(config_dir)]
    model = ["--model", plan["model"]] if plan.get("model") else []
    effort = ["--effort", plan["effort"]] if plan.get("effort") else []
    return [plan["binary"], *terminal_mode(sandbox), *extra, "--settings",
            json.dumps(settings, separators=(",", ":")), *model, *effort,
            *(["--conversation", conversation] if conversation else ["-i", prompt])]


def _mcp_menu(rows):
    return ("MCP Servers" in rows
            and any(row.startswith("Keyboard: ↑/↓ Navigate") and "esc Exit" in row for row in rows))


def _ready_mcp_servers(rows):
    if not _mcp_menu(rows):
        return set()
    return {
        match[1] for row in rows
        if (match := re.match(r"^(?:>\s+)?✓\s+(\S+)\s+Tools:\s+\S", row))
    }


def _deliver_first_task(process, pane, identity, task_file, menu_file, receipt, servers, deadline):
    resume = task_file is None
    stage = "opening"
    waiting_on = servers

    def tmux(*args):
        command = ([sys.executable, str(_home() / "bin/lib/runtime_tmux_consumer.py")]
                   if args[0] in {"submit-input", "send-keys"} else ["tmux"])
        return subprocess.run([*command, *args], capture_output=True, encoding="utf-8",
                              timeout=max(0.1, min(8, deadline - time.monotonic())))

    def same_pane():
        reply = tmux("display-message", "-p", "-t", pane, TMUX_FORMAT)
        return reply.returncode == 0 and reply.stdout.strip() == identity

    while process.poll() is None and time.monotonic() < deadline:
        if stage == "confirming":
            if receipt.is_file():
                return
            time.sleep(0.15)
            continue
        if not same_pane():
            raise ValueError("The Antigravity pane changed before its first task")
        rows = {row.strip() for row in tmux("capture-pane", "-p", "-t", pane).stdout.splitlines()}
        if stage == "opening" and _mcp_menu(rows):
            stage = "waiting"
        if stage == "waiting":
            waiting_on = servers - _ready_mcp_servers(rows)
            if not waiting_on and same_pane():
                if tmux("send-keys", "-t", pane, "Escape").returncode:
                    raise ValueError("Antigravity's MCP manager could not be closed")
                stage = "submitting"
        elif stage == "opening":
            # submit-input already checks the actual editor and refuses while
            # agy is restoring its UI or while an operator has a draft.
            result = tmux("submit-input", pane, str(menu_file),
                          *(("--prepare-agy-mcp",) if resume else ()))
            if result.returncode not in (0, 3, 4):
                raise ValueError("Antigravity's MCP manager could not be opened safely")
            # A native menu can be visible even when an older input helper
            # cannot confirm that Enter opened it.
            if result.returncode in (0, 4):
                stage = "waiting"
        elif stage == "submitting" and not _mcp_menu(rows):
            if resume:
                return
            result = tmux("submit-input", pane, str(task_file))
            if result.returncode in (0, 4):
                if receipt.is_file():
                    return
                stage = "confirming"
            elif result.returncode != 3:
                raise ValueError("Antigravity did not confirm submission of its first task")
        time.sleep(0.15)
    if stage == "waiting" and waiting_on:
        names = ", ".join(sorted(waiting_on))
        raise ValueError(f"Antigravity MCP tools did not become ready ({names}). Check /mcp and try again.")
    if stage == "confirming":
        raise ValueError("Antigravity did not acknowledge its first task in PreInvocation.")
    if resume:
        raise ValueError("Antigravity did not open its MCP manager after resuming.")
    raise ValueError("Antigravity did not accept its first task. Check CLI sign-in and workspace trust.")


def _run_first_task(argv, *, env, runtime, name, identity, servers):
    startup = _startup_path(runtime, name)
    child_resume._atomic_json(startup, {"tmux": identity})
    try:
        with tempfile.TemporaryDirectory(prefix="agy-first-task-", dir=runtime.resolve()) as directory:
            folder = Path(directory)
            menu, task, receipt = (folder / filename for filename in ("menu", "task", "acknowledged"))
            trigger = "Begin the ORRERY task " + uuid.uuid4().hex[:12]
            menu.write_text("/mcp", encoding="utf-8")
            task.write_text(trigger, encoding="utf-8")
            child_resume._atomic_bytes(folder / "payload", argv[-1].encode("utf-8"))
            env = {**env, "AGENTSTACK_AGY_FIRST_TASK_DIR": directory,
                   "AGENTSTACK_AGY_FIRST_TASK_TRIGGER": trigger}
            process = subprocess.Popen(argv[:-2], env=env)
            try:
                _deliver_first_task(process, env["TMUX_PANE"], identity, task, menu, receipt, set(servers),
                                    time.monotonic() + STARTUP_TIMEOUT)
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                raise
        startup.unlink()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, ValueError) else "Antigravity's MCP startup check could not run"
        child_resume._atomic_json(startup, {"tmux": identity, "error": message})
        raise ValueError(message) from error
    return process.wait()


def _run_managed_resume(argv, *, env, runtime, name, identity, servers, previous_history):
    startup = _startup_path(runtime, name)
    pane = env["TMUX_PANE"]
    state = {"tmux": identity, "conversation": argv[-1],
             "generation": child_resume.tools_change_generation()}
    child_resume._atomic_json(startup, state)
    subprocess.run(["tmux", "set-option", "-p", "-t", pane, MCP_PREPARING, "1"],
                   check=True, timeout=5)
    process = subprocess.Popen(argv, env=env)
    try:
        with tempfile.TemporaryDirectory(prefix="agy-resume-", dir=runtime.resolve()) as directory:
            menu = Path(directory) / "menu"
            menu.write_text("/mcp", encoding="utf-8")
            _deliver_first_task(process, pane, identity, None, menu, None, set(servers),
                                time.monotonic() + STARTUP_TIMEOUT)
        _history()["confirm_resume"](runtime, name, env["AGENTSTACK_AGY_HISTORY_LAUNCH"],
                                     argv[-1], previous_history)
        subprocess.run(["tmux", "set-option", "-pu", "-t", pane, MCP_PREPARING],
                       check=True, timeout=5)
        child_resume._atomic_json(startup, {**state, "ready": True})
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, ValueError) else "Antigravity's MCP resume check could not run"
        child_resume._atomic_json(startup, {**state, "error": message})
        print(message, file=sys.stderr)
    finally:
        # Keep the original conversation open for manual inspection on failure.
        result = process.wait()
        startup.unlink(missing_ok=True)
    return result


def run(runtime, name, task_file=None, conversation=None):
    path, token, _child_home, _mcp, _lock = child_resume._paths(runtime, name)
    state = child_resume._load_state(path)
    project = os.environ["AGENTSTACK_PROJECT_KEY"]
    child_resume._validate_identity(state, agent_name=name, project_key=project, program=PROGRAM)
    child_resume._check_tools_change(state, path)
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
               AGENTSTACK_GEMINI_TMUX_IDENTITY=identity)
    parent = launch_parent(state)
    if parent:
        env["PARENT_AGENT"] = parent
    else:
        env.pop("PARENT_AGENT", None)
        env.pop("AGENTSTACK_PROXY_PARENT_AGENT", None)
    if conversation and not plan.get("standalone"):
        excludes = runtime / f"gemini-git-excludes-{name}"
        child_resume._atomic_bytes(excludes, (f".agents/mcp_config.json\n.agents/hooks.json\n{TOOL_REFUSAL_RULE}\n").encode())
        count = env.get("GIT_CONFIG_COUNT", "0")
        index = int(count) if count.isdecimal() else 0
        env[f"GIT_CONFIG_KEY_{index}"] = "core.excludesFile"
        env[f"GIT_CONFIG_VALUE_{index}"] = str(excludes)
        env["GIT_CONFIG_COUNT"] = str(index + 1)

    os.chdir(cwd)
    status_path = _status_path(runtime, name)
    status_path.unlink(missing_ok=True)
    config_dir = workspace_mcp = written_mcp = mcp = None
    try:
        standalone = plan.get("standalone") is True
        if not standalone:
            write_tool_refusal_rule(cwd)
        changed = permission_selection(runtime, state, conversation) if conversation else None
        spec = changed["selection"] if changed else _tool_selection(plan)
        screen_mode = "off"
        if conversation and not standalone:
            if changed:
                workspace_mcp = _workspace_mcp_snapshot(runtime, state, cwd)
            else:
                # Older delegated sessions may use project-owned MCP files.
                # Only rebuild a configuration whose managed origin is verified.
                with suppress(OSError, ValueError):
                    workspace_mcp = _workspace_mcp_snapshot(runtime, state, cwd)
        if standalone or conversation is None or changed or workspace_mcp:
            mcp_path = (workspace_mcp[0] if workspace_mcp else
                        None if standalone else _delegated_mcp_path(cwd))
            change_screen = changed and changed.get("computer_use_changed") and child_resume.tools_change_generation()
            generation = uuid.uuid4().hex
            mcp, screen_mode = child_tools.agy_launch_config(
                _mail_server()["command"], spec, runtime=str(runtime), project=project, child=name,
                mail_env=os.environ.get("AGENTSTACK_MAIL_ENV", str(_home() / "mail" / ".env")),
                generation=generation, cwd=cwd if standalone else None,
                resume_from=(plan.get("screen_generation") or "") if conversation and not change_screen else None,
            )
            _eager_managed_servers(mcp, spec, screen_mode)
            if standalone:
                directory = path.with_suffix(".agy-config")
                _configure_standalone(directory, plan, mcp)
                config_dir = directory
            else:
                child_resume._atomic_json(mcp_path, mcp)
                written_mcp = mcp_path.read_bytes()
                snapshots = [workspace_mcp[1], written_mcp] if workspace_mcp else [written_mcp]
                plan = {**plan, "workspace_mcp_hashes": [hashlib.sha256(raw).hexdigest() for raw in snapshots]}
            plan = {**plan, "screen_generation": generation if screen_mode is not None else None,
                    "screen_launch_mode": screen_mode or "off", "screen_tmux": identity}
        if screen_mode is not None or conversation or written_mcp:
            child_resume.prepare_active_state(runtime, name, project_key=project, program=PROGRAM,
                                              gemini_launch=plan)
        previous_history = None
        if conversation and mcp is not None:
            registration = dict(agent_id=state["agent_id"], project_key=project, program=PROGRAM)
            previous_history = _history()["binding"](runtime, name, registration)
            if previous_history.get("history_session_id") != conversation:
                raise ValueError("Antigravity's saved conversation changed before resume")
        env["AGENTSTACK_AGY_HISTORY_LAUNCH"] = subprocess.check_output(
            [sys.executable, str(history_helper()), "prepare", str(state["agent_id"]), identity],
            env=env, text=True, timeout=5).strip()
        argv = command(plan, cwd, prompt=prompt, conversation=conversation, config_dir=config_dir,
                       screen_mode=screen_mode, parent=parent, selection=spec)
        if prompt is not None:
            return _run_first_task(argv, env=env, runtime=runtime, name=name, identity=identity,
                                   servers=mcp["mcpServers"])
        if previous_history is not None:
            return _run_managed_resume(argv, env=env, runtime=runtime, name=name, identity=identity,
                                       servers=mcp["mcpServers"], previous_history=previous_history)
        return subprocess.run(argv, env=env).returncode
    finally:
        try:
            if workspace_mcp and written_mcp is not None:
                mcp_path, previous = workspace_mcp
                if not mcp_path.is_symlink() and mcp_path.is_file() and mcp_path.read_bytes() == written_mcp:
                    child_resume._atomic_bytes(mcp_path, previous)
        finally:
            if config_dir is not None:
                shutil.rmtree(config_dir, ignore_errors=True)
            status_path.unlink(missing_ok=True)
            subprocess.run(["/bin/bash", str(_home() / "hooks" / "cleanup-child-agent.sh"), name],
                           env=env, check=False)


def _status_context(window):
    if not isinstance(window, dict):
        return {}
    size = window.get("context_window_size")
    if type(size) is not int or size <= 0:
        return {}
    context = {"ctx_window_tokens": size}
    used = window.get("used_percentage")
    if isinstance(window.get("current_usage"), dict) and type(used) in (int, float) and 0 <= used <= 100:
        context["ctx_used"] = int(used + 0.5)
    return context


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
              "conversation": old.get("conversation"),
              **_status_context(payload.get("context_window"))}
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


def _workspace_protected_roots(cwd, runtime):
    install = Path(os.environ.get("AGENTSTACK_HOME") or _home()).resolve()
    worktrees = Path(os.environ.get("AGENTSTACK_WORKTREE_ROOT") or install / "worktrees").resolve()
    # An explicitly selected managed worktree can live under the installation.
    if cwd != worktrees and cwd.is_relative_to(worktrees) and not install.is_relative_to(worktrees):
        return (runtime.resolve(),)
    return runtime.resolve(), install


def guard(event, cwd, *, runtime):
    tool = event["toolCall"]
    name, args = tool["name"], tool["args"]
    inputs = {}
    overwrite = None
    protected = _workspace_protected_roots(cwd, runtime)
    if name in WRITE_TOOLS:
        target = Path(args["TargetFile"])
        target = (cwd / target).resolve()
        if (args.get("IsArtifact") or not target.is_relative_to(cwd)
                or any(target.is_relative_to(root) for root in protected)
                or any(part.casefold() == ".git" for part in target.relative_to(cwd).parts)
                    or target == (cwd / TOOL_REFUSAL_RULE).resolve()
                or target in {(cwd / ".agents" / filename).resolve()
                              for filename in ("hooks.json", "mcp_config.json")}):
            return {"decision": "deny", "reason": "Edit files only in the child worktree"}
        inputs["file_path"] = str(target)
        mode = WRITE_TOOLS[name]
    elif name == "run_command":
        workdir = (cwd / Path(args.get("Cwd") or cwd)).resolve()
        if not workdir.is_relative_to(cwd) or any(workdir.is_relative_to(root) for root in protected):
            return {"decision": "deny", "reason": "Run the command inside the child worktree"}
        if args.get("BypassSandbox") and os.environ.get("AGENTSTACK_CHILD_SANDBOX") == "on":
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
            reason = getattr(result, "stderr", "").strip()
            if hook == "deny-llm-key-read.py" and reason.startswith("MACOS_CONSENT_DENIED:"):
                return {"decision": "deny", "reason": reason}
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
    setup.add_argument("--standalone", action="store_true")
    setup.add_argument("--tools", action="append", default=[])
    launch = actions.add_parser("run")
    group = launch.add_mutually_exclusive_group(required=True)
    group.add_argument("--task-file", type=Path)
    group.add_argument("--conversation")
    actions.add_parser("status")
    actions.add_parser("guard")
    headless = actions.add_parser("exec-headless")
    headless.add_argument("binary")
    headless.add_argument("arguments", nargs=argparse.REMAINDER)
    consent = actions.add_parser("consent-hook")
    consent.add_argument("cwd", type=Path)
    rule = actions.add_parser("tool-refusal-rule")
    rule.add_argument("cwd", type=Path)
    for action in ("ready", "wake-ready"):
        ready = actions.add_parser(action)
        ready.add_argument("name")
        ready.add_argument("identity")
    args = parser.parse_args()
    if args.action == "consent-hook":
        write_headless_consent_hook(args.cwd.resolve(strict=True))
        return 0
    if args.action == "tool-refusal-rule":
        write_tool_refusal_rule(args.cwd.resolve(strict=True))
        return 0
    if args.action == "exec-headless":
        sandbox = os.environ.get("AGENTSTACK_CHILD_SANDBOX") == "on"
        settings = json.dumps(terminal_settings(Path.home(), sandbox), separators=(",", ":"))
        os.execvp(args.binary, [args.binary, *terminal_mode(sandbox), "--settings", settings, *args.arguments])
    runtime = Path(os.environ["AGENTSTACK_RUNTIME_DIR"])
    if args.action in {"ready", "wake-ready"}:
        startup = _startup_state(runtime, args.name, args.identity)
        if startup:
            if args.action == "ready" and startup.get("error"):
                print(startup["error"])
                return 2
            if not startup.get("ready"):
                return 1
        activity = read_status(runtime, args.name, args.identity).get("activity")
        return 0 if activity == "wait" or (args.action == "ready" and activity) else 1
    name = os.environ["AGENT_NAME"]
    if args.action == "prepare":
        prepare(runtime, name, os.environ["AGENTSTACK_PROJECT_KEY"], os.environ.get("PARENT_AGENT", ""),
                source=args.source, cwd=args.cwd, binary=args.binary, model=args.model, effort=args.effort,
                standalone=args.standalone, tools=args.tools)
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
                             Path(state["gemini_launch"]["cwd"]), runtime=runtime)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
            decision = {"decision": "deny", "reason": "ORRERY could not check the child worktree"}
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except child_tools.ToolsError as error:
        sys.exit(f"Error: {error}")
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(f"Antigravity child: {error}")
