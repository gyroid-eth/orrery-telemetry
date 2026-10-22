"""Native instructions share coordination rules without borrowing an identity."""
import hashlib
import importlib.util
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('neutral_instruction_blocks', ROOT / 'bin/lib/instruction_blocks.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
SHARED = (ROOT / 'codex/AGENTS.md').read_bytes()


def render(template=SHARED):
    return renderer.render('harness', template, '/fixture/install', '/fixture/project')


def test_neutral_identity_and_all_shared_sections_are_kept_byte_for_byte():
    output = render()
    assert not any(word in output.lower() for word in (b'codex', b'claude-code', b'claude code'))
    assert b'__ORRERY_' not in output and b'<!-- include:' not in output
    template = (ROOT / 'bin/lib/harness_instructions.md').read_bytes()
    includes = re.findall(rb'^<!-- include: (.+) UNTIL (.+) -->$', template, re.M)
    assert len(includes) == 5
    expanded = renderer.render('codex', SHARED, '/fixture/install', '/fixture/project')
    for start, end in includes:
        section = expanded[expanded.index(start + b'\n'):expanded.index(end + b'\n')]
        assert section in output
    for heading in (b'## Coordination (ORRERY Mail)', b'## File reservations', b'## Waiting For Replies',
                    b'## Reading Notifications', b'## Canonical coordination paths are fail-closed'):
        assert heading in output
    assert renderer.parse(output)[2] == 'harness'


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'reversed'])
def test_shared_boundary_changes_fail_instead_of_silently_dropping_rules(mutation):
    start = b'### 1. Embedded canonical task'
    end = b'### 3. Raw/direct connection after shell registration'
    if mutation == 'missing':
        broken = SHARED.replace(start, b'### Renamed embedded task')
    elif mutation == 'duplicate':
        broken = SHARED + b'\n' + start + b'\n'
    else:
        broken = SHARED.replace(start, b'__SWAP__').replace(end, start).replace(b'__SWAP__', end)
    with pytest.raises(renderer.Refused, match='INSTRUCTION_SHARED_SECTION_INVALID'):
        render(broken)


@pytest.mark.parametrize('provider,name,digest', [
    ('claude', 'CLAUDE.md', '11631ed22c0999b0f527fcd95171cb1334fc356645b7cb66a68a27f5badca740'),
    ('codex', 'AGENTS.md', '26ef5d3cf1f404f8bc10d9d7a9eb7060d65e0bd610efc85d333eab80691fef14'),
])
def test_program_output_matches_scope_adjusted_baseline(provider, name, digest):
    # Issue #33 intentionally changes reservation guidance, not the provider
    # renderer. Pin the new complete output to catch unrelated template drift.
    output = renderer.render(provider, (ROOT / provider / name).read_bytes(), '/fixture/install', '/fixture/project')
    assert hashlib.sha256(output).hexdigest() == digest


def test_installed_layout_can_render_without_the_source_checkout(tmp_path):
    from test_gemini_core_install_boundary import _run_install_payload

    install = tmp_path / 'install'
    result = _run_install_payload(install)
    assert result.returncode == 0, result.stderr
    home = tmp_path / 'home'
    home.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    code = '''import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('installed_harness',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
output=module.write_instructions(Path(sys.argv[2]),'/fixture/project')
sys.stdout.buffer.write(output.read_bytes())
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code,
        str(install / 'hooks/harness_child.py'), str(state)], cwd='/',
        env={'HOME':str(home),'PATH':'/usr/bin:/bin'}, stdin=subprocess.DEVNULL,
        capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == renderer.render('harness', SHARED, str(install), '/fixture/project')
    assert (state / 'instructions.md').stat().st_mode & 0o777 == 0o600
