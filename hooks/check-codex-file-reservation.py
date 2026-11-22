#!/usr/bin/env python3
"""Adapt Codex apply_patch input to the shared Edit/Write reservation guard."""
from __future__ import annotations

from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


# Match Codex rust-v0.159.2 streaming_parser.rs whitespace and state rules.
# In particular Add/Delete headers trim both ends; Update context keeps its
# leading space, and a filename's leading spaces belong to the filename.
_RUST_WHITESPACE = "\t\n\r\v\f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"


def patch_targets(command: str) -> list[tuple[str, str]]:
    lines = command.split('\n')
    state, targets, update_index, chunks = 'start', [], None, False
    for index, raw in enumerate(lines):
        # Native push_delta removes CR only on newline-terminated lines.
        line = raw.removesuffix('\r') if index < len(lines) - 1 else raw
        trimmed = line.strip(_RUST_WHITESPACE)
        if state == 'start':
            if trimmed != '*** Begin Patch':
                raise ValueError('expected a complete apply_patch document')
            state = 'headers'
            continue
        if state == 'end':
            if trimmed:
                raise ValueError('content after End Patch')
            continue
        header = line.rstrip(_RUST_WHITESPACE) if state == 'update' else trimmed
        # Native finish recognizes an unterminated final End Patch after trim.
        if header == '*** End Patch' or (index == len(lines) - 1 and trimmed == '*** End Patch'):
            state = 'end'
            continue
        for marker, mode, operation in (
            ('*** Add File: ', 'add', 'Edit'),
            ('*** Delete File: ', 'delete', 'Delete'),
            ('*** Update File: ', 'update', 'Edit'),
        ):
            if header.startswith(marker):
                path = header[len(marker):]
                if not path or '\x00' in path:
                    raise ValueError('empty or invalid patch path')
                targets.append((path, operation))
                state, update_index, chunks = mode, len(targets) - 1, False
                break
        else:
            if state == 'update':
                if not chunks and targets[update_index][1] == 'Edit' and header.startswith('*** Move to: '):
                    path = header[len('*** Move to: '):]
                    if not path or '\x00' in path:
                        raise ValueError('empty or invalid move path')
                    targets[update_index] = (targets[update_index][0], 'MoveSource')
                    targets.append((path, 'Edit'))
                elif line == '' or line.startswith((' ', '+', '-')) or header == '@@' or header.startswith('@@ ') or header == '*** End of File':
                    chunks = True
                else:
                    raise ValueError('unsupported update line')
            elif state != 'add' or not line.startswith('+'):
                raise ValueError('unsupported patch line')
    if state != 'end' or not targets:
        raise ValueError('expected a complete patch with targets')
    return list(dict.fromkeys(targets))


def patch_paths(command: str) -> list[str]:
    return list(dict.fromkeys(path for path, _operation in patch_targets(command)))


def run_shared_guard(guard: Path, document: dict, cwd: str, timeout: float):
    process = subprocess.Popen(['/bin/bash', str(guard)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, cwd=cwd, start_new_session=True)
    try:
        stdout, stderr = process.communicate(json.dumps(document), timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill only this invocation's unreaped process group, including a
        # helper retaining the pipes. Killing just bash can leave communicate
        # waiting for a descendant, past Codex's fail-open hook timeout.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def main() -> int:
    try:
        raw = sys.stdin.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('hook input exceeds 1 MiB')
        payload = json.loads(raw)
        if payload.get('tool_name') != 'apply_patch':
            raise ValueError('expected apply_patch hook input')
        command = payload['tool_input']['command']
        if not isinstance(command, str):
            raise ValueError('tool_input.command must be patch text')
        targets = patch_targets(command)
        cwd = payload.get('cwd')
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ValueError('absolute, existing payload cwd required')
        guard = Path(__file__).with_name('check-file-reservation.sh')
        if not guard.is_file():
            raise ValueError('shared reservation guard unavailable')
        deadline = time.monotonic() + 25
        messages = []
        for path, operation in targets:
            # Resolve symlink aliases and .. before the shared protected-root check.
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = Path(cwd) / candidate
            # Delete and Move remove the directory entry, even when its final
            # component is a symlink pointing outside the protected root.
            if operation in ('Delete', 'MoveSource'):
                candidate = candidate.parent.resolve() / candidate.name
            else:
                candidate = candidate.resolve()
            document = dict(payload, tool_name=operation, tool_input={'file_path': str(candidate)})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('reservation checks exceeded their time budget')
            result = run_shared_guard(guard, document, cwd, remaining)
            if result.stderr:
                print(result.stderr, file=sys.stderr, end='')
            if result.returncode:
                return 2
            if result.stdout.strip():
                # Preserve shared warn-open diagnostics as one valid hook JSON object.
                for line in result.stdout.splitlines():
                    try:
                        warning = json.loads(line).get('systemMessage')
                    except (ValueError, AttributeError):
                        continue
                    if isinstance(warning, str) and warning not in messages:
                        messages.append(warning)
        if messages:
            print(json.dumps({'systemMessage': '\n'.join(messages)}))
        return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f'CODEX RESERVATION GUARD: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
