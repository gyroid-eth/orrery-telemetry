#!/usr/bin/env python3
"""Match shell credential reads against existing, non-secret model definitions.

Never executes the command/auth helper or reads credential values. This is a
mistake guard, not a shell sandbox: encoded commands and arbitrary scripts are
outside its scope.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys

SHELL_TOOLS = {'Bash', 'exec_command', 'shell_command', 'shell'}
MESSAGE = 'LOCAL_MODEL_KEY_READ_DENIED: do not read model credentials in the agent shell; use the managed launcher/auth runner.'


def substitutions(command):
    """Ordinary $(...) and backticks, including double-quoted assignments."""
    i, quote = 0, None
    while i < len(command):
        c = command[i]
        if c == '\\' and quote != "'":
            i += 2
            continue
        if c == "'" and quote != '"':
            quote = None if quote == "'" else "'"
        elif c == '"' and quote != "'":
            quote = None if quote == '"' else '"'
        elif quote != "'" and command.startswith('$(', i):
            start, depth, inner_quote = i + 2, 1, None
            j = start
            while j < len(command) and depth:
                v = command[j]
                if v == '\\' and inner_quote != "'":
                    j += 2
                    continue
                if v in "'\"" and inner_quote in (None, v):
                    inner_quote = None if inner_quote else v
                elif inner_quote is None:
                    depth += (v == '(') - (v == ')')
                j += 1
            if depth:
                raise ValueError('incomplete substitution')
            yield command[start:j - 1]
            i = j
            continue
        elif c == '`' and quote != "'":
            end = command.find('`', i + 1)
            if end < 0:
                raise ValueError('incomplete backtick')
            yield command[i + 1:end]
            i = end + 1
            continue
        i += 1


def commands(command, depth=0):
    if depth > 5:
        raise ValueError('nested shell')
    for inner in substitutions(command):
        yield from commands(inner, depth + 1)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()<>\n')
    lexer.whitespace = ' \t\r'
    words = []
    for token in [*lexer, ';']:
        if token and all(c in ';&|()<>\n' for c in token):
            if words:
                while words and re.match(r'^[A-Za-z_][A-Za-z_0-9]*=', words[0]):
                    words.pop(0)
                while words and Path(words[0]).name in {'env', 'command', 'sudo', 'timeout'}:
                    wrapper = Path(words.pop(0)).name
                    while words and (words[0].startswith('-') or '=' in words[0]):
                        option = words.pop(0)
                        if option in {'-u', '--unset', '-C', '--chdir', '-k', '--kill-after', '-s', '--signal'} and words:
                            words.pop(0)
                    if wrapper == 'timeout' and words:
                        words.pop(0)  # duration
                if words:
                    yield words
                    if Path(words[0]).name in {'bash', 'sh', 'zsh', 'dash'}:
                        for n, arg in enumerate(words[1:], 1):
                            if arg.startswith('-') and 'c' in arg and n + 1 < len(words):
                                yield from commands(words[n + 1], depth + 1)
                                break
            words = []
        else:
            words.append(token)


def extraction(words):
    """OS-specific extraction forms; the rest of the guard is shared."""
    name = Path(words[0]).name
    if name == 'security' and 'find-generic-password' in words:
        tail = words[words.index('find-generic-password') + 1:]
        attrs, read = {}, False
        i = 0
        while i < len(tail):
            arg = tail[i]
            if arg.startswith('-') and not arg.startswith('--'):
                flags = arg[1:]
                while flags:
                    flag, flags = flags[0], flags[1:]
                    if flag in 'wg':
                        read = True
                    elif flag in 'sa':
                        if not flags:
                            i += 1
                            if i >= len(tail):
                                raise ValueError('missing selector')
                            flags = tail[i]
                        attrs[flag], flags = flags, ''
                        break
            i += 1
        return ('keychain', attrs) if read else None
    if name == 'secret-tool' and len(words) > 1 and words[1] == 'lookup':
        args = words[2:]
        if len(args) % 2:
            raise ValueError('incomplete secret selector')
        return 'secret-tool', dict(zip(args[::2], args[1::2]))
    return None


def definition_paths():
    explicit = os.environ.get('AGENTSTACK_LLM_KEY_DEFINITIONS')
    if explicit:
        root = Path(explicit).expanduser()
        if root.is_dir():
            return sorted(root.glob('*.toml'))
        return [root]
    core = Path(os.environ.get('AGENTSTACK_HOME') or Path(__file__).resolve().parent.parent)
    candidate = core / 'integrations' / 'dsh' / 'backend.json'
    try:
        candidate.stat()
    except FileNotFoundError:
        return []
    return [candidate]


def read_definitions(paths):
    refs, auth_commands = [], []
    for path in paths:
        meta = path.stat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > 1024 * 1024:
            raise ValueError('invalid definition')
        raw = path.read_text(encoding='utf-8')
        if path.suffix == '.toml':
            import tomllib
            data = tomllib.loads(raw)
            adapters = [data.get(k, {}) for k in ('claude', 'codex')]
        else:
            data = json.loads(raw)
            backends = data['backends']
            if not isinstance(backends, dict):
                raise ValueError('invalid backends')
            adapters = list(backends.values())
        for adapter in adapters:
            if not isinstance(adapter, dict):
                raise ValueError('invalid adapter')
            keychain = adapter.get('keychain')
            if keychain is not None:
                if not isinstance(keychain, dict) or not isinstance(keychain.get('service'), str) or not keychain['service']:
                    raise ValueError('invalid credential reference')
                account = keychain.get('account') or ''
                if not isinstance(account, str):
                    raise ValueError('invalid credential account')
                refs.append(('keychain', {'s': keychain['service'], 'a': account}))
            auth = adapter.get('auth_command')
            if auth is not None:
                if not isinstance(auth, list) or not auth or not all(isinstance(a, str) and a for a in auth):
                    raise ValueError('invalid auth command')
                auth_commands.append(auth)
                form = extraction(auth)
                if form:
                    refs.append(form)
    return refs, auth_commands


def same_key(candidate, reference):
    kind, attrs = candidate
    other_kind, wanted = reference
    if kind != other_kind:
        return False
    # Omitting a selector broadens a lookup and can return the protected key.
    return all(not attrs.get(k) or '$' in attrs[k] or '`' in attrs[k] or not v or attrs[k] == v for k, v in wanted.items())


def command_text(payload):
    name = payload.get('tool_name', '').rsplit('.', 1)[-1]
    if name not in SHELL_TOOLS:
        return None
    args = payload.get('tool_input') or {}
    if isinstance(args, str):
        args = json.loads(args)
    value = args.get('command', args.get('cmd'))
    if isinstance(value, list):
        return shlex.join(value)
    if not isinstance(value, str):
        raise ValueError('missing shell command')
    return value


def decide(payload):
    text = command_text(payload)
    if text is None:
        return False
    # Parsing errors must not stop unrelated shell commands.
    shape = ('find-generic-password' in text and bool(re.search(r'-[a-zA-Z]*[wg]', text))) or ('secret-tool' in text and 'lookup' in text)
    forms = None
    try:
        paths = definition_paths()
        if not paths:
            return False
        invocations = list(commands(text))
        forms = [form for words in invocations if (form := extraction(words))]
        refs, auth_commands = read_definitions(paths)
        for words in invocations:
            if any(words[:len(auth)] == auth for auth in auth_commands):
                return True
        return any(same_key(form, ref) for form in forms for ref in refs)
    except Exception:
        return bool(forms) if forms is not None else bool(shape)


def main():
    raw = sys.stdin.read()
    try:
        denied = decide(json.loads(raw))
    except Exception:
        denied = ('find-generic-password' in raw and ('-w' in raw or '-g' in raw)) or ('secret-tool' in raw and 'lookup' in raw)
    if denied:
        print(MESSAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
