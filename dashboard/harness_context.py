"""Read native context status from an already captured pane, without I/O.

Only the active footer is observed. Missing/hidden/truncated status is unknown;
model names and cumulative billed tokens never supply a context estimate.
"""
from decimal import Decimal
import re

_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
_COUNT = r'\d+(?:\.\d+)?[kKmM]?'
_PAIR = rf'(?P<used>{_COUNT})\s*/\s*(?P<window>{_COUNT})'
_DSH = re.compile(
    rf'\bctx\s+(?:(?P<pct>\d+(?:\.\d+)?)%\s+\({_PAIR}\)'
    rf'|(?P<used2>{_COUNT})/(?P<window2>{_COUNT})\s+\((?P<pct2>\d+(?:\.\d+)?)%\))')
_HERMES = re.compile(rf'☤\s+.+?\s+[·│]\s+(?P<estimated>~?){_PAIR}(?:\s+pinned)?\s+[·│]\s+(?:\[[█░]*\]\s+)?~?(?P<pct>\d+)%')
_HERMES_READY_PREFIX = r'^\s*[─━]+\s*ready\s*│'
_HERMES_READY = re.compile(
    rf'{_HERMES_READY_PREFIX}\s*[^│]+│\s*~?{_PAIR}\s*│\s*\[[█░]+\]\s*'
    r'(?:│[^─━]*)?[─━]+')
_DSH_EFFORT = re.compile(
    r'^[a-zA-Z0-9][a-zA-Z0-9._/-]*…?'
    r'(?:\s+·\s+| {2,})(none|minimal|low|medium|high|xhigh|max|ultra|default)'
    r'(?:\s+·\s+| {2,})cach(?:e\b|…)')


def window_tokens(label, *, allow_zero=False):
    """Expand a reported token count at the precision of the displayed label."""
    if not isinstance(label, str) or len(label) > 18 or not re.fullmatch(_COUNT, label):
        return None
    scale = {'K': 1000, 'M': 1000000}.get(label[-1].upper(), 1)
    value = Decimal(label[:-1] if scale != 1 else label) * scale
    return int(value) if (value > 0 or allow_zero) and value == int(value) else None


def dsh_effort(text):
    """Read an untruncated effort in the native footer after the composer."""
    lines = _ANSI.sub('', text).splitlines()[-12:]
    composer = next((i for i in range(len(lines) - 1, -1, -1)
                     if re.match(r'^\s*⌸\s+❯', lines[i])), None)
    if composer is None:
        return None
    for line in reversed(lines[composer + 1:]):
        if _DSH.search(line):
            match = _DSH_EFFORT.match(line.strip())
            return match[1] if match else None
    return None


def parse_context(text, program):
    """Return the existing ctx_used (percent) / ctx_window (display size).

    dsh-tui 0.16.1: ``ctx 19% (24k/128k)`` or its noncompact inverse.
    Hermes pinned source: ``☤ model ~24k/128K [bar] ~19% ...``.
    Hermes estimates stay estimates; this function does not infer activity,
    running state, model identity or exact token counts from rounded labels.
    """
    unknown = {'ctx_used': None, 'ctx_window': None}
    if program not in ('dsh', 'hermes') or not isinstance(text, str):
        return unknown
    lines = _ANSI.sub('', text).splitlines()[-12:]
    # The newest footer wins; old captures above the current one are ignored.
    for line in reversed(lines):
        if program == 'hermes' and re.match(_HERMES_READY_PREFIX, line):
            ready = _HERMES_READY.match(line)
            if ready:
                window = window_tokens(ready['window'])
                used = window_tokens(ready['used'], allow_zero=True)
                if window and used is not None and used * 100 <= window * 999:
                    return {'ctx_used': (used * 200 + window) // (window * 2),
                            'ctx_window': ready['window'].upper()}
            return unknown
        match = (_DSH.search(line) if program == 'dsh' else _HERMES.search(line))
        if not match:
            continue
        groups = match.groupdict()
        window = groups.get('window') or groups.get('window2')
        percent = float(groups.get('pct') or groups.get('pct2'))
        if float(window.rstrip('kKmM')) <= 0 or percent > 999:
            return unknown
        return {'ctx_used': int(percent + .5), 'ctx_window': window.upper()}
    return unknown
