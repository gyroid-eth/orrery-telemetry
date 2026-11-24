"""Minimal prompt for tests that submit through a private real tmux server."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time


def editor_command(received):
    return shlex.join([sys.executable, '-I', '-B', str(Path(__file__).resolve()), str(received)])


def wait_for_prompt(command, pane, *, env=None):
    deadline = time.monotonic() + 10
    screen = ''
    while time.monotonic() < deadline:
        screen = subprocess.check_output([*command, 'capture-pane', '-p', '-t', pane],
                                         env=env, text=True, timeout=5)
        if any(line.strip() == '❯' for line in screen.splitlines()):
            return
        time.sleep(.03)
    raise AssertionError(f'Fixture prompt did not appear: {screen!r}')


def editor(received):
    import tty

    begin, end = b'\x1b[200~', b'\x1b[201~'
    prompt = b'\x1b[2J\x1b[H' + '❯ '.encode()
    tty.setraw(0)
    os.write(1, b'\x1b[?2004h' + prompt)
    while True:
        raw = bytearray()
        while end not in raw:
            chunk = os.read(0, 4096)
            if not chunk:
                return
            raw.extend(chunk)
        pasted, _, early = raw.partition(end)
        assert pasted.startswith(begin) and not early, 'Enter must wait for the visible draft'
        text = pasted[len(begin):].decode()
        os.write(1, text.replace('\n', '\r\n').encode())
        assert os.read(0, 1) == b'\r', 'Expected Enter after the completed paste'
        with received.open('a', encoding='utf-8') as output:
            output.write(json.dumps(text) + '\n')
        os.write(1, prompt)


if __name__ == '__main__':
    editor(Path(sys.argv[1]))
