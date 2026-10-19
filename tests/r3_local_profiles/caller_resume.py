"""TEST-only real spawn/end precondition for the actual resume caller.

A minimal transcript is fixture input, not a native CLI observation. Only the
real SessionStart/R2/end APIs may create the session, receipt and retained state.
"""
import hashlib
import json
import os
from pathlib import Path
import uuid

from .caller_binding import read_binding_trace, assert_single_replacement, assert_real_delivery_gates
from .evidence import (canonical_digest, intent_preimage, pinned_capture_provider,
                       read_fresh_capture, assert_runtime_capture)


def checked_saved_intent(owner, expected):
    intent = json.loads(owner.read_text())['launch_intent']
    assert intent['intent_digest'] == canonical_digest(intent_preimage(intent)), \
        'resume precondition intent digest mismatch'
    if expected is not None:
        assert intent == expected, 'resume precondition changed approved intent'
    return intent


def set_fixture_binding(probe, enabled):
    """Change only the private TEST bootstrap switch for a later subprocess."""
    paths = list((probe.workspace.tmp/'delivery-python/lib').glob('python*/site-packages/delivery-fixture.json'))
    assert len(paths) == 1
    path = paths[0]
    assert path.resolve() == path and path.stat().st_uid == os.getuid()
    assert path.stat().st_mode & 0o777 == 0o400
    value = json.loads(path.read_text())
    assert value['install'] == str(probe.install) and value['trace'] == str(probe.trace)
    assert type(enabled) is bool
    value['enabled'] = enabled
    path.chmod(0o600)
    try:
        path.write_text(json.dumps(value, sort_keys=True))
    finally:
        path.chmod(0o400)


def prepare_real_resume(probe, data, *, approved_intent, options):
    ws = probe.workspace
    owner = Path(options['owner'])
    # The all-five positive supplies its prior intent. Only isolated rejection
    # controls or the focused resume-precondition test may start without one.
    assert (approved_intent is not None or not probe.enabled
            or options.get('expect_tools_rejection') or options.get('resume_precondition_only')), \
        'resume has no explicit approved intent input'
    saved = checked_saved_intent(owner, approved_intent)
    session_id = data['provider_session_id']
    assert session_id == 'native-fixture-session'
    binary = Path(data['provider_binary'])
    dispatch = binary.with_name('fixture-provider-dispatch.json')
    original_dispatch = dispatch.read_bytes()
    current = json.loads(original_dispatch)
    current_capture = Path(current['script']).with_suffix('.json')
    assert not current_capture.exists() and not current_capture.with_suffix('.driver.json').exists()
    nonce = uuid.uuid4().hex
    capture = ws.tmp/'captures'/('resume-precondition-'+nonce+'.json')
    before_trace = len(read_binding_trace(probe.trace))
    driver_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    if not probe.enabled:
        # A genuine retained state needs a genuine preceding delivery. Its
        # binding evidence must not be confused with the actual disabled resume.
        set_fixture_binding(probe, True)
    try:
        pinned_capture_provider(ws, 'prepare-child', nonce, capture)
        script = capture.with_suffix('.py')
        lines = script.read_text().splitlines(keepends=True)
        transcript = owner.parent/'runtime/provider-home/codex/sessions/fixture.jsonl'
        prefix = (
            'import json, os, subprocess\nfrom pathlib import Path\n'
            f'_transcript = Path({str(transcript)!r})\n'
            '_transcript.parent.mkdir(mode=0o700, parents=True, exist_ok=True)\n'
            f'_transcript.write_text(json.dumps({{"type":"session_meta","payload":{{"id":{session_id!r}}}}})+"\\n")\n'
            '_transcript.chmod(0o600)\n'
            f'_payload = {{"session_id":{session_id!r},"transcript_path":str(_transcript),'
            '"cwd":os.getcwd(),"provider_pid":os.getpid()}\n'
            f'_result = subprocess.run([{str(probe.python)!r}, '
            f'{str(probe.install/"bin/lib/runtime_client.py")!r}, "entry", "session-start", '
            f'"--context", {data["child_context"]!r}], input=json.dumps(_payload), '
            'text=True, capture_output=True, timeout=20)\n'
            'assert _result.returncode == 0, "REAL_SESSION_START_REJECTED: "+_result.stderr\n'
        )
        script.chmod(0o600)
        script.write_text(lines[0]+prefix+''.join(lines[1:]))
        script.chmod(0o500)
        expected_script_hash = hashlib.sha256(script.read_bytes()).hexdigest()
        temporary = dispatch.with_suffix('.tmp')
        temporary.write_text(json.dumps({'script':str(script), 'sha256':expected_script_hash}))
        temporary.chmod(0o600)
        os.replace(temporary, dispatch)
        result = probe.run_entry('resume-precondition', data)
        rows = read_binding_trace(probe.trace)[before_trace:]
        assert_single_replacement(rows, enabled=True)
        assert result.returncode == 0, 'R3_REAL_RESUME_PRECONDITION_REJECTED: '+result.stderr
        assert_real_delivery_gates(rows, probe.install)
        observed = read_fresh_capture(capture, nonce)
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == driver_hash
        assert_runtime_capture(observed, owner.parent/'runtime/launch.json', capture=capture,
                               binary=binary, expected_script_sha256=expected_script_hash)
        launch = json.loads((owner.parent/'runtime/launch.json').read_text())
        state = json.loads((owner.parent/'runtime/child-state.json').read_text())
        session = json.loads((owner.parent/'runtime/session_index/self.json').read_text())
        assert launch['launch_kind'] == 'startup' and launch['phase'] == 'ended'
        assert launch['termination']['phase'] == 'ended'
        assert launch['execution']['outcome'] == {'state':'exited', 'returncode':0}
        assert state['phase'] == 'retained'
        assert session['session_id'] == session_id and session['transcript_path'] == str(transcript)
        assert json.loads(transcript.read_text()) == {'type':'session_meta','payload':{'id':session_id}}
        checked_saved_intent(owner, saved)
        probe.resume_precondition = {'intent':saved, 'launch':launch, 'state':state,
                                     'session':session, 'capture':capture, 'rows':rows}
        assert not current_capture.exists() and not current_capture.with_suffix('.driver.json').exists()
    finally:
        temporary = dispatch.with_suffix('.tmp')
        temporary.write_bytes(original_dispatch)
        temporary.chmod(0o600)
        os.replace(temporary, dispatch)
        if not probe.enabled:
            set_fixture_binding(probe, False)
