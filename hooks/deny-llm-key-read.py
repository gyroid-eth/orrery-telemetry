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

SHELL_TOOLS = {'bash', 'exec_command', 'shell_command', 'shell', 'terminal'}
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


def commands(command, depth=0, executables=()):
    """Scan the whole command for extraction forms, independent of shell prefixes.

    Quoted data remains one token. Grep/rg arguments are data, while their
    command substitutions are scanned separately. This deliberately does not
    attempt to execute or fully interpret shell control flow.
    """
    if depth > 5:
        raise ValueError('nested shell')
    for inner in substitutions(command):
        yield from commands(inner, depth + 1, executables)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()<>\n')
    lexer.whitespace = ' \t\r'
    words = []
    for token in [*lexer, ';']:
        if token and all(c in ';&|()<>\n' for c in token):
            for i, word in enumerate(words):
                name = Path(word).name
                if name in {'grep', 'rg'}:
                    break
                if name in {'security', 'secret-tool'} or word in executables:
                    yield words[i:]
                if name in {'bash', 'sh', 'zsh', 'dash'}:
                    for n in range(i + 1, len(words) - 1):
                        if words[n].startswith('-') and 'c' in words[n]:
                            yield from commands(words[n + 1], depth + 1, executables)
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
    if name == 'secret-tool' and len(words) > 1 and words[1] in {'lookup', 'search'}:
        args = words[2:]
        if words[1] == 'search':
            # GOption removes options anywhere before --, before secret-tool
            # forms attribute/value pairs. Only tokens after -- are literal.
            selectors, options = [], True
            for arg in args:
                if options and arg in {'--all', '--unlock'}:
                    continue
                elif options and arg == '--':
                    options = False
                else:
                    if options and arg.startswith('-'):
                        raise ValueError('unknown secret search option')
                    selectors.append(arg)
            args = selectors
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
            credential_ref = adapter.get('credential_ref')
            if credential_ref is not None:
                if (not isinstance(credential_ref, dict) or credential_ref.get('store') != 'secret-service'
                        or not isinstance(credential_ref.get('attributes'), dict)
                        or not credential_ref['attributes']
                        or not all(isinstance(k, str) and k and isinstance(v, str) and v
                                   for k, v in credential_ref['attributes'].items())):
                    raise ValueError('invalid credential reference')
                refs.append(('secret-tool', credential_ref['attributes']))
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
    name = payload.get('tool_name', '').rsplit('.', 1)[-1].lower()
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


def protected_file_read(payload, text):
    """Compare references only: never open the credential/provider files."""
    raw = os.environ.get('AGENTSTACK_LLM_KEY_FILES')
    if not raw:
        return False
    paths = json.loads(raw)
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise ValueError('invalid protected file references')
    cwd = Path(payload.get('cwd') or os.getcwd())
    def resolved(value):
        path = Path(os.path.expandvars(value)).expanduser()
        return (path if path.is_absolute() else cwd / path).resolve()
    protected = {resolved(path) for path in paths}
    args = payload.get('tool_input') or {}
    if isinstance(args, str):
        args = json.loads(args)
    name = payload.get('tool_name', '').rsplit('.', 1)[-1].lower()
    if name in {'read', 'read_file', 'view', 'file_read'}:
        path = args.get('file_path', args.get('path'))
        if not isinstance(path, str):
            raise ValueError('missing read path')
        return resolved(path) in protected
    if text is None:
        return False
    # Ordinary shell file reads, including redirects and quoted path operands.
    # Like the existing shell guard this does not interpret encoded scripts.
    lexer = shlex.shlex(text, posix=True, punctuation_chars=';&|()<>')
    lexer.whitespace_split = True
    for token in lexer:
        if token and not all(c in ';&|()<>' for c in token):
            if resolved(token) in protected:
                return True
            name, separator, value = token.partition('=')
            if separator and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                # A normal FILE="$HOME/credential" assignment names the file
                # before its later use; no shell evaluation is needed.
                if resolved(value) in protected:
                    return True
    return False


def decide(payload):
    text = command_text(payload)
    if protected_file_read(payload, text):
        return True
    if text is None:
        return False
    # Parsing errors must not stop unrelated shell commands.
    shape = ('find-generic-password' in text and bool(re.search(r'-[a-zA-Z]*[wg]', text))) or ('secret-tool' in text and any(verb in text for verb in ('lookup', 'search')))
    forms = None
    try:
        paths = definition_paths()
        if not paths:
            return False
        invocations = list(commands(text))
        forms = [form for words in invocations if (form := extraction(words))]
        refs, auth_commands = read_definitions(paths)
        readers = {auth[3] for auth in auth_commands if len(auth) >= 4 and auth[1:3] == ['-I', '-B']
                   and Path(auth[3]).name in ('llm_secret_reader.py', 'native_web_search.py')}
        for words in commands(text, executables={auth[0] for auth in auth_commands} | readers):
            if any(words[:len(auth)] == auth for auth in auth_commands):
                return True
            # Python flags are not part of the credential identity. Detect an
            # ordinary alternate invocation of the selected internal reader.
            if any(word in readers for word in words):
                return True
        return any(same_key(form, ref) for form in forms for ref in refs)
    except Exception:
        return bool(forms) if forms is not None else bool(shape)


def main():
    raw = sys.stdin.read()
    try:
        denied = decide(json.loads(raw))
    except Exception:
        denied = ('find-generic-password' in raw and ('-w' in raw or '-g' in raw)) or ('secret-tool' in raw and any(verb in raw for verb in ('lookup', 'search')))
    if denied:
        print(MESSAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
