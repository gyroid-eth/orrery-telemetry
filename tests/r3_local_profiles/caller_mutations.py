"""Explicit TEST-only real-entry mutants, never a normal delivery binding."""
import hashlib
import json
import os
from pathlib import Path


NOOP_TARGETS = {'prepare-child':'RuntimeClient.prepare_child',
                'resume-child':'RuntimeClient.resume_child', 'operator':'RuntimeOperator.spawn'}


def apply_noop(data):
    """Only the actual invocation subprocess calls this, never setup/spawn."""
    if data['entry'] == 'dashboard':
        return apply_dashboard_noop(data)
    from runtime_client import RuntimeClient
    from runtime_operator import RuntimeOperator
    mutation = data['real_entry_mutation']
    assert mutation['kind'] == 'no-op' and data['entry'] in NOOP_TARGETS
    target = NOOP_TARGETS[data['entry']]
    cls = RuntimeOperator if data['entry'] == 'operator' else RuntimeClient
    method = target.split('.')[1]
    original = getattr(cls, method)
    trace = Path(mutation['trace'])
    assert not trace.exists() and trace.parent.name.startswith('r3-caller-')
    def record(event):
        flags = os.O_WRONLY|os.O_NOFOLLOW
        flags |= (os.O_CREAT|os.O_EXCL) if event=='installed' else os.O_APPEND
        fd = os.open(trace, flags, 0o600)
        try:
            os.write(fd, (json.dumps({'event':event, 'target':target,
                'method_file':original.__code__.co_filename, 'pid':os.getpid()})+'\n').encode())
        finally:
            os.close(fd)
    def noop(self, *args, **kwargs):
        record('activated')
        return {'ok':True,'prepared':True,'runtime_ready':False,
                'client_config':data['child_context']}
    setattr(cls, method, noop)
    assert getattr(cls, method) is noop
    record('installed')


def apply_dashboard_noop(data):
    """Explicit negative: only the actual HTTP dispatch root is replaced."""
    import inspect
    from dashboard import server
    mutation = data['real_entry_mutation']
    assert mutation['kind'] == 'no-op'
    original = server.do_spawn
    expected = Path(data['provider_binary']).parent.parent/'install/dashboard/server.py'
    assert original.__module__ == 'dashboard.server' and original.__qualname__ == 'do_spawn'
    assert original.__code__.co_filename == str(expected)
    before = {name:value for name,value in vars(server).items() if inspect.isfunction(value)}
    trace = Path(mutation['trace'])
    assert not trace.exists() and trace.parent.name.startswith('r3-caller-')
    replacements = []
    def record(event):
        flags = os.O_WRONLY|os.O_NOFOLLOW
        flags |= (os.O_CREAT|os.O_EXCL) if event=='installed' else os.O_APPEND
        fd = os.open(trace, flags, 0o600)
        try:
            os.write(fd, (json.dumps({'event':event, 'target':'dashboard.server.do_spawn',
                'method_file':original.__code__.co_filename, 'method_name':original.__qualname__,
                'module':original.__module__, 'pid':os.getpid(),
                'replacements':replacements, 'replacement_count':len(replacements)})+'\n').encode())
        finally:
            os.close(fd)
    def noop(payload):
        record('activated')
        return {'ok':True}
    server.do_spawn = noop
    after = {name:value for name,value in vars(server).items() if inspect.isfunction(value)}
    assert before.keys() == after.keys()
    replacements = sorted(server.__name__+'.'+name for name in before if before[name] is not after[name])
    assert replacements == ['dashboard.server.do_spawn']
    record('installed')


def emit_previous_capture(binary, current, previous):
    """Change only an owned external-command input, keeping fixed driver bytes."""
    assert current.parent == previous.parent and current.parent.name == 'captures'
    assert not current.exists() and not current.with_suffix('.driver.json').exists()
    assert previous.is_file() and not previous.is_symlink()
    script = current.with_suffix('.py')
    content = script.read_text().splitlines(keepends=True)
    script.chmod(0o600)
    script.write_text(content[0]+'import os\nfrom pathlib import Path\n'
        +f'Path({str(current)!r}).write_bytes(Path({str(previous)!r}).read_bytes())\n'
        +f'Path({str(current.with_suffix(".stale-pid"))!r}).write_text(str(os.getpid()))\n')
    script.chmod(0o500)
    expected = hashlib.sha256(script.read_bytes()).hexdigest()
    dispatch = binary.with_name('fixture-provider-dispatch.json')
    temporary = dispatch.with_suffix('.tmp')
    temporary.write_text(json.dumps({'script':str(script),'sha256':expected}))
    temporary.chmod(0o600)
    os.replace(temporary, dispatch)
    return expected
