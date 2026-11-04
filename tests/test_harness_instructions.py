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


def test_native_task_rules_are_short_and_omit_orrery_child_creation_procedures():
    output = render()
    # A compact launch document, without changing native helper capabilities.
    assert len(output.split()) < 1000
    assert not any(command in output for command in (
        b'agentstack-preregister-child', b'spawn_child.sh', b'### Raw/direct connection and recovery',
        b'## Deterministic daemon development'))
    for rule in (b'private scratch', b'outside the project', b'ttl_seconds', b'600',
                 b'reserve_files', b'renew_reservations', b'release_reservations',
                 b'caller identity', b'fetch_inbox', b'polling', b'exact failure'):
        assert rule in output


def test_native_rendered_reservation_guidance_matches_managed_guard():
    output = render()
    reservations = output.split(b'## File reservations', 1)[1].split(b'## Messaging Other Agents', 1)[0]
    for guidance in (b'workspace-v1', b'physical absolute paths',
                     b'resolve symlinks first', b'same paths',
                     b'renew_reservations', b'release_reservations',
                     b'relative paths and globs', b'unchanged',
                     b'namespace', b'protected roots'):
        assert guidance in reservations
    assert b'project-relative paths' not in reservations


def test_neutral_identity_and_all_shared_sections_are_kept_byte_for_byte():
    output = render()
    assert not any(word in output.lower() for word in (b'codex', b'claude-code', b'claude code'))
    assert b'__ORRERY_' not in output and b'<!-- include:' not in output
    template = (ROOT / 'bin/lib/harness_instructions.md').read_bytes()
    includes = re.findall(rb'^<!-- include: (.+) UNTIL (.+) -->$', template, re.M)
    # Keep the common startup, fail-closed, pointer and notification rules.
    # Recovery and ORRERY child-creation procedures are outside this document.
    assert len(includes) == 4
    expanded = renderer.render('codex', SHARED, '/fixture/install', '/fixture/project')
    for start, end in includes:
        section = expanded[expanded.index(start + b'\n'):expanded.index(end + b'\n')]
        assert section in output
    for heading in (b'## Coordination (ORRERY Mail)', b'## File reservations', b'## Waiting For Replies',
                    b'## Reading Notifications', b'## Canonical coordination paths are fail-closed'):
        assert heading in output
    assert renderer.parse(output)[2] == 'harness'


@pytest.mark.parametrize('start,end', re.findall(
    rb'^<!-- include: (.+) UNTIL (.+) -->$',
    (ROOT / 'bin/lib/harness_instructions.md').read_bytes(), re.M))
@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'reversed'])
def test_shared_boundary_changes_fail_instead_of_silently_dropping_rules(mutation, start, end):
    if mutation == 'missing':
        broken = SHARED.replace(start, b'### Renamed embedded task')
    elif mutation == 'duplicate':
        broken = SHARED + b'\n' + start + b'\n'
    else:
        broken = SHARED.replace(start, b'__SWAP__').replace(end, start).replace(b'__SWAP__', end)
    with pytest.raises(renderer.Refused, match='INSTRUCTION_SHARED_SECTION_INVALID'):
        render(broken)


@pytest.mark.parametrize('provider,name,digest', [
    ('claude', 'CLAUDE.md', '39a95b17bd3957cc355795b556737dbc0ff5589f5d7f33c8613b6a33b6b993e9'),
    ('codex', 'AGENTS.md', '59728f94e5cf4ebee3d05d5e01d2ffed6034335c640129bc018b5cb16c751081'),
])
def test_program_output_matches_scope_adjusted_baseline(provider, name, digest):
    # Reviewed against af1e33d0: each provider template adds exactly four lines
    # requiring physical absolute paths in managed workspace-v1 sessions,
    # the same paths for renew/release, unchanged legacy paths/globs, and no
    # inferred cwd. The renderer is unchanged; pin the intended full output.
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
