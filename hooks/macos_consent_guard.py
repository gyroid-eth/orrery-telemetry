#!/usr/bin/env python3
"""Recognize common shell and interpreter forms that could request macOS consent.

This pre-execution mistake guard reads command text and explicitly invoked
source files. It never executes commands or requests a macOS permission.
"""

import ast
import json
import os
import re
import shlex
import sys
from pathlib import Path

BLOCKED = {"screencapture", "osascript"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
PREFIXES = {"command", "exec", "env", "nohup", "nice", "time"}
QUARTZ = {
    "CGWindowListCreateImage", "CGWindowListCreateImageFromArray",
    "CGDisplayCreateImage", "CGDisplayCreateImageForRect",
    "CGRequestScreenCaptureAccess", "CGDisplayStreamCreate",
    "CGDisplayStreamCreateWithDispatchQueue",
}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
PYTHON = re.compile(r"^(?:python|pypy)(?:[23](?:\.\d+)?)?$")
LEX = re.compile(r"""(?P<comment>#[^\n]*)|(?P<sep>&&|\|\||\|&|[;\n|&()])|(?P<redir><<<|<<-?|>>|[<>])|(?P<word>(?:\x60[^\x60]*\x60|'[^']*'|"(?:\\.|[^"\\])*"|\\[\s\S]|[^\x60 \t\r\n;|&()<>"'\\])+)""")


def token_words(raw):
    return shlex.split(raw, comments=False)


def source_at(cwd, value):
    candidate = cwd / Path(value).expanduser()
    try:
        if candidate.is_file() and candidate.stat().st_size <= 512 * 1024:
            return candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        pass
    return ""


def python_capture(source, cwd, depth):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return False
    functions = set(QUARTZ)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"Quartz", "CoreGraphics"}:
            functions.update(name.asname for name in node.names if name.name in QUARTZ and name.asname)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", getattr(func, "id", None))
        if name in functions:
            return True
        owner = getattr(getattr(func, "value", None), "id", None)
        if owner not in {"subprocess", "os"} or not node.args:
            continue
        if name not in {"run", "Popen", "call", "check_call", "check_output", "system"}:
            continue
        try:
            command = ast.literal_eval(node.args[0])
        except (ValueError, TypeError, SyntaxError, RecursionError):
            continue
        if isinstance(command, str) and inspect_shell(command, cwd, depth + 1):
            return True
        if isinstance(command, (list, tuple)) and all(isinstance(x, str) for x in command):
            if inspect_argv(list(command), cwd, "", depth + 1):
                return True
    return False


def swift_capture(source):
    code = re.sub(r'/\*[\s\S]*?\*/|//[^\n]*|"""[\s\S]*?"""|"(?:\\.|[^"\\])*"', " ", source)
    return bool(re.search(r"\b(?:" + "|".join(sorted(QUARTZ)) + r")\s*\(", code))


def inspect_argv(argv, cwd, stdin, depth):
    if not argv or depth > 8:
        return False
    index = 0
    while index < len(argv):
        word = argv[index]
        if ASSIGN.match(word) or word in PREFIXES or word in {"!", "then", "do", "else", "if", "elif", "while", "until", "{"}:
            index += 1
            continue
        if index and word in {"-u", "--unset"} and argv[0] == "env":
            index += 2
            continue
        if index and word in {"-v", "-V"} and argv[index - 1] == "command":
            return False
        if index and word.startswith("-"):
            index += 1
            continue
        break
    if index == len(argv):
        return False
    executable = Path(argv[index]).name
    options = argv[index + 1:]
    if executable in BLOCKED:
        return True
    if executable in SHELLS | {"eval"} or PYTHON.fullmatch(executable) or executable in {"swift", "swiftc"}:
        language = executable
    else:
        # Explicitly invoked local scripts can also have a shell/Python/Swift shebang.
        if "/" not in argv[index]:
            return False
        source = source_at(cwd, argv[index])
        if not source.startswith("#!"):
            return False
        shebang = token_words(source.splitlines()[0][2:])
        language = Path(shebang[-1]).name if shebang else ""
        stdin = source
        options = []
    if language == "eval":
        return inspect_shell(" ".join(options), cwd, depth + 1)
    source = stdin
    for i, arg in enumerate(options):
        if arg in {"-c", "-e"} or (language in SHELLS and re.fullmatch(r"-[ilxvc]*c[ilxv]*", arg)):
            source = options[i + 1] if i + 1 < len(options) else ""
            break
        if arg == "-":
            break
        if not arg.startswith("-"):
            source = source_at(cwd, arg)
            break
    if language in SHELLS:
        return inspect_shell(source, cwd, depth + 1)
    if PYTHON.fullmatch(language):
        return python_capture(source, cwd, depth)
    if language in {"swift", "swiftc"}:
        return swift_capture(source)
    return False


def inspect_shell(source, cwd, depth=0):
    if not source or depth > 8:
        return False
    lines = iter(source.splitlines(keepends=True))
    code = ""
    for line in lines:
        code += line
        if code.endswith("\\\n"):
            continue
        try:
            shlex.split(code, comments=True)
        except ValueError:
            # The command can contain a quoted string spanning several lines.
            continue
        chunks = list(LEX.finditer(code))
        argv, heredoc, redirection = [], "", None
        for token in chunks:
            field, raw = token.lastgroup, token.group()
            if field == "sep":
                if inspect_argv(argv, cwd, heredoc, depth):
                    return True
                if len(argv) == 2 and argv[0] == "cd":
                    cwd = cwd / Path(argv[1]).expanduser()
                argv, heredoc, redirection = [], "", None
            elif field == "redir":
                redirection = raw
            elif field == "word":
                words = token_words(raw)
                if not words:
                    continue
                if redirection in {"<<", "<<-"}:
                    delimiter = words[0]
                    here = []
                    for content in lines:
                        candidate = content.lstrip("\t") if redirection == "<<-" else content
                        if candidate.rstrip("\r\n") == delimiter:
                            break
                        here.append(content)
                    heredoc = "".join(here)
                elif redirection == "<<<":
                    heredoc = words[0]
                elif redirection is None:
                    argv.extend(words)
                    for quoted in re.finditer(r"""'[^']*'|"(?:\\.|[^"\\])*"|`[^`]*`""", raw):
                        quote = quoted.group()
                        if quote.startswith("'"):
                            continue
                        if quote.startswith("`") and inspect_shell(quote[1:-1], cwd, depth + 1):
                            return True
                        for pair in re.findall(r"(?<!\\)\$\(([^()]*)\)|(?<!\\)\x60([^\x60]*)\x60", quote):
                            expression = pair[0] or pair[1]
                            if inspect_shell(expression, cwd, depth + 1):
                                return True
                redirection = None
        if inspect_argv(argv, cwd, heredoc, depth):
            return True
        code = ""
    return False


MESSAGE = (
    "MACOS_CONSENT_DENIED: tmux cannot request macOS screen or Automation consent. "
    "Use Cua Driver for the desktop, or chrome MCP / ~/.claude/tools/cdp/cdp.py for the browser."
)


def check(command, cwd):
    if sys.platform != "darwin" or not os.environ.get("TMUX") or not isinstance(command, str):
        return None
    try:
        return MESSAGE if inspect_shell(command, Path(cwd)) else None
    except (ValueError, TypeError, OSError, RecursionError):
        return None


def antigravity_settings(python, script):
    command = shlex.join([python, str(script), "--antigravity"])
    return {"orrery-consent": {"PreToolUse": [{
        "matcher": "^run_command$",
        "hooks": [{"type": "command", "command": command, "timeout": 5}],
    }]}}


def main():
    if sys.argv[1:] == ["--antigravity"]:
        event = json.load(sys.stdin)
        tool = event.get("toolCall") or {}
        inputs = tool.get("args") or {}
        reason = None
        if tool.get("name") == "run_command":
            cwd = inputs.get("Cwd") or os.getcwd()
            command = inputs.get("CommandLine")
            reason = check(command, cwd)
            from shell_guard_marks import record
            record(command, event)
        print(json.dumps({"decision": "deny", "reason": reason} if reason else {"decision": "allow"}))
        return 0

    event = json.load(sys.stdin)
    if event.get("tool_name") != "Bash":
        return 0
    inputs = event.get("tool_input") or {}
    cwd = Path(event.get("cwd") or os.getcwd()) / (inputs.get("workdir") or "")
    command = inputs.get("command")
    reason = check(command, cwd)
    from shell_guard_marks import record
    record(command, event)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
