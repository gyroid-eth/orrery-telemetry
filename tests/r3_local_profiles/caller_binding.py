"""Delivery-only TEST bootstrap; not a native CLI capability binding.

The private interpreter keeps this one replacement across R2's minimal-env
exec. It never changes the host interpreter, installed services or native38.
"""
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import sysconfig
import venv


def _delivery_library_source():
    """Only the selected Darwin shared build's declared library, no search."""
    if (sys.platform!='darwin' or sysconfig.get_config_var('PYTHONFRAMEWORK')
            or not sysconfig.get_config_var('Py_ENABLE_SHARED')):
        return None
    directory=sysconfig.get_config_var('LIBDIR')
    name=sysconfig.get_config_var('LDLIBRARY')
    if (not isinstance(directory,str) or not Path(directory).is_absolute()
            or not isinstance(name,str) or Path(name).name!=name
            or not name.startswith('libpython') or not name.endswith('.dylib')):
        raise RuntimeError('R3_DELIVERY_PYTHON_UNSUPPORTED: library-metadata')
    source=Path(directory)/name
    try:
        if not source.resolve(strict=True).is_file():raise OSError()
    except (OSError,RuntimeError,ValueError):
        raise RuntimeError('R3_DELIVERY_PYTHON_UNSUPPORTED: library-unavailable') from None
    # Keep the loader's declared basename even when the base uses a symlink.
    # The private destination itself must always be a regular file.
    return source


def _copy_delivery_library(root):
    source=_delivery_library_source()
    if source is None:return []
    canonical=source.resolve(strict=True)
    target=root/'lib'/source.name
    try:target.parent.mkdir(mode=0o700,exist_ok=True)
    except OSError:
        raise RuntimeError('R3_DELIVERY_PYTHON_UNSUPPORTED: library-copy') from None
    if (target.parent.resolve()!=target.parent or target.exists() or target.is_symlink()):
        raise RuntimeError('R3_DELIVERY_PYTHON_LIBRARY_INVALID')
    try:
        digest=hashlib.sha256(canonical.read_bytes()).hexdigest()
        shutil.copyfile(canonical,target)
        target.chmod(0o500)
    except OSError:
        raise RuntimeError('R3_DELIVERY_PYTHON_UNSUPPORTED: library-copy') from None
    resources=[{'path':str(target),'source':str(canonical),'sha256':digest}]
    _verify_delivery_library(root,resources)
    return resources


def _verify_delivery_library(root,resources):
    source=_delivery_library_source()
    if source is None:
        if resources!=[]:raise RuntimeError('R3_DELIVERY_PYTHON_LIBRARY_INVALID')
        return
    target=root/'lib'/source.name
    if (not isinstance(resources,list) or len(resources)!=1 or not isinstance(resources[0],dict)
            or set(resources[0])!={'path','source','sha256'}):
        raise RuntimeError('R3_DELIVERY_PYTHON_LIBRARY_INVALID')
    expected=resources[0]
    try:
        info=target.lstat()
        if (root.resolve()!=root or target.resolve()!=target
                or not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o500
                or expected['path']!=str(target)
                or expected['source']!=str(source.resolve(strict=True))):
            raise RuntimeError('R3_DELIVERY_PYTHON_LIBRARY_INVALID')
        if (hashlib.sha256(source.read_bytes()).hexdigest()!=expected['sha256']
                or hashlib.sha256(target.read_bytes()).hexdigest()!=expected['sha256']):
            raise RuntimeError('R3_DELIVERY_PYTHON_LIBRARY_CHANGED')
    except OSError:
        raise RuntimeError('R3_DELIVERY_PYTHON_LIBRARY_INVALID') from None


def _private_python_sites(python):
    try:
        result=subprocess.run([str(python),'-c',
            'import json,site; print(json.dumps(site.getsitepackages()))'],
            capture_output=True,text=True,check=True,timeout=20)
    except subprocess.CalledProcessError as error:
        raise RuntimeError('R3_DELIVERY_PYTHON_UNSUPPORTED: probe-exit-'+str(error.returncode)) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError('R3_DELIVERY_PYTHON_UNSUPPORTED: probe-timeout') from None
    except OSError:
        raise RuntimeError('R3_DELIVERY_PYTHON_UNSUPPORTED: probe-exec') from None
    try:
        values=json.loads(result.stdout)
        if not isinstance(values,list) or any(not isinstance(path,str) for path in values):raise ValueError()
        sites=[Path(path) for path in values if Path(path).is_dir()]
        if (len(sites)!=1 or sites[0].resolve()!=sites[0]
                or not sites[0].is_relative_to(python.parent.parent)):raise ValueError()
    except (ValueError,TypeError,OSError):
        raise RuntimeError('R3_DELIVERY_PYTHON_UNSUPPORTED: probe-output') from None
    return sites


def make_delivery_python(workspace, install, *, enabled=True):
    """Regular copies, including the selected build's required Darwin dylib."""
    root = workspace.tmp / 'delivery-python'
    assert not root.exists() and root.resolve() == root
    try:venv.EnvBuilder(with_pip=False, symlinks=False).create(root)
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):
        raise RuntimeError('R3_DELIVERY_PYTHON_UNSUPPORTED: venv-create') from None
    python = root / 'bin/python'
    assert python.is_file() and not python.is_symlink()
    assert python.read_bytes() == Path(sys.executable).resolve(strict=True).read_bytes()
    resources=_copy_delivery_library(root)
    sites=_private_python_sites(python)
    site = sites[0]
    source = Path(__file__).resolve()
    target = site / '_orrery_delivery_fixture.py'
    shutil.copyfile(source, target)
    target.chmod(0o500)
    trace = workspace.tmp / 'delivery-binding.jsonl'
    config = {'install': str(install), 'trace': str(trace), 'enabled': enabled,
              'bootstrap_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'interpreter_resources':resources}
    (site / 'delivery-fixture.json').write_text(json.dumps(config, sort_keys=True))
    (site / 'delivery-fixture.json').chmod(0o400)
    # Homebrew supplies sitecustomize.py in stdlib, ahead of the venv's site
    # directory. A private .pth startup import avoids shadowing/modifying it.
    (site / 'orrery-delivery-fixture.pth').write_text(
        'import _orrery_delivery_fixture; _orrery_delivery_fixture.startup()\n')
    (site / 'orrery-delivery-fixture.pth').chmod(0o400)
    return python, trace


def _functions(modules):
    """Snapshot callable identities, including methods, without replacing them."""
    result = {}
    for module in modules:
        for name, value in vars(module).items():
            if inspect.isfunction(value):
                result[module.__name__ + '.' + name] = value
            elif inspect.isclass(value) and value.__module__ == module.__name__:
                for method, function in vars(value).items():
                    if inspect.isfunction(function):
                        result[module.__name__ + '.' + name + '.' + method] = function
    return result


def startup():
    # Python otherwise reports startup-hook exceptions and continues. This
    # harness must stop the private interpreter instead of silently unbinding.
    try:
        # The real installer can copy preexisting checkout bytecode. Imports
        # must not rewrite those owned payload bytes during a read-only probe.
        sys.dont_write_bytecode = True
        bootstrap(__file__)
    except BaseException as error:
        if isinstance(error,RuntimeError) and str(error).startswith('R3_DELIVERY_PYTHON_'):
            os.write(2,str(error).encode()[:512]+b'\n')
        os.write(2, b'DELIVERY_FIXTURE_BOOTSTRAP_FAILED\n')
        os._exit(86)


def bootstrap(startup_file):
    """Executed ONLY in the fixture venv, including its R2 exec children."""
    # site adds a venv directory twice in some supported Python versions.
    # Assert identities once per interpreter, not once per .pth evaluation.
    if getattr(bootstrap, '_loaded', False):
        return
    bootstrap._loaded = True
    site = Path(startup_file).parent
    config = json.loads((site / 'delivery-fixture.json').read_text())
    assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == config['bootstrap_sha256']
    _verify_delivery_library(site.parents[2],config['interpreter_resources'])
    install = Path(config['install'])
    assert install.resolve() == install and install.name == 'install'
    library = install / 'bin/lib'
    sys.path.insert(0, str(install))
    sys.path.insert(0, str(library))
    names = ('runtime_native_scope', 'runtime_intent_store', 'runtime_launch_intent',
             'runtime_local_launch', 'runtime_delivery', 'runtime_client')
    modules = [importlib.import_module(name) for name in names]
    for module in modules:
        assert Path(module.__file__).resolve() == library / (module.__name__ + '.py')
    scope = modules[0]
    assert scope.require_native_binding.__module__ == 'runtime_native_scope'
    before = _functions(modules)

    def record(event, **data):
        # Never record arguments, locals, env, requests or credentials.
        value = {'event': event, 'pid': os.getpid(), **data}
        fd = os.open(config['trace'], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(value, sort_keys=True) + '\n').encode())
        finally:
            os.close(fd)

    def delivery_only(plan):
        # This rejects use by non-fixture plans; it does not interpret or
        # enforce native permissions and does not mark the plan as verified.
        assert plan['declared_cli_version'] == 'fixture-pinned'
        assert plan['requires_native_validation'] is True
        assert plan['launch_admissible'] is False
        record('delivery-only-native-gate', target='runtime_native_scope.require_native_binding')

    assert type(config['enabled']) is bool
    if config['enabled']:
        scope.require_native_binding = delivery_only
    after = _functions(modules)
    assert set(before) == set(after)
    changed = sorted(name for name in before if before[name] is not after[name])
    expected = ['runtime_native_scope.require_native_binding'] if config['enabled'] else []
    assert changed == expected
    record('bootstrap', replacements=changed, replacement_count=len(changed))

    observed = set()
    library_prefix = str(library)+os.sep
    entry_files = {str(install/'hooks/child_tools.py'), str(install/'dashboard/server.py'),
                   str(install/'dashboard/http_operator.py'), str(install/'dashboard/global_backend.py')}
    shell_noop_installed = False
    def observe(frame, event, arg):
        nonlocal shell_noop_installed
        if event != 'call':
            return
        # This runs for every Python call, including Mail's dependency stack.
        # Filter the same exact installed paths before constructing any objects.
        file = frame.f_code.co_filename
        if not (file.startswith(library_prefix) or file in entry_files):
            return
        key = (file, frame.f_code.co_name)
        if key not in observed:
            observed.add(key)
            record('real-call', file=key[0], name=key[1])
        if (config.get('shell_noop') and file==str(library/'runtime_client.py')
                and frame.f_code.co_name=='main' and frame.f_globals.get('__name__')=='__main__'
                and sys.argv[1:3]==['entry','spawn-child']):
            # Explicit negative only, after the real preregister prerequisite.
            # Patch the actual CLI root rather than an internal task helper.
            assert config['enabled'] is True and not shell_noop_installed
            mutation = config['shell_noop']
            assert set(mutation)=={'kind','trace'} and mutation['kind']=='no-op'
            trace = Path(mutation['trace'])
            assert trace==install.parent/'mutation-shell.jsonl' and not trace.exists()
            assert trace.parent.resolve()==trace.parent and trace.parent.name.startswith('r3-caller-')
            original = frame.f_globals['entry_operation']
            assert original.__code__.co_filename==str(library/'runtime_client.py')
            assert original.__globals__ is frame.f_globals and original.__qualname__=='entry_operation'
            before_entry = {name:value for name,value in frame.f_globals.items() if inspect.isfunction(value)}
            def mutation_record(stage):
                flags = os.O_WRONLY|os.O_NOFOLLOW
                flags |= (os.O_CREAT|os.O_EXCL) if stage=='installed' else os.O_APPEND
                fd = os.open(trace,flags,0o600)
                try:
                    os.write(fd,(json.dumps({'event':stage,'target':'runtime_client.entry_operation',
                        'method_file':original.__code__.co_filename,'method_name':original.__qualname__,
                        'module':original.__module__,'entry':'spawn-child','pid':os.getpid()})+'\n').encode())
                finally:os.close(fd)
            def no_shell(entry, argv):
                assert entry=='spawn-child'
                mutation_record('activated')
                return 0
            frame.f_globals['entry_operation'] = no_shell
            after_entry = {name:value for name,value in frame.f_globals.items() if inspect.isfunction(value)}
            assert set(before_entry)==set(after_entry)
            changed_entry = sorted(name for name in before_entry if before_entry[name] is not after_entry[name])
            assert changed_entry==['entry_operation']
            shell_noop_installed = True
            mutation_record('installed')
            record('shell-entry-mutation',replacements=['runtime_client.entry_operation'],replacement_count=1)
    assert sys.getprofile() is None
    sys.setprofile(observe)


def read_binding_trace(trace):
    return [json.loads(line) for line in trace.read_text().splitlines()]


def assert_single_replacement(rows, *, enabled=True):
    starts = [row for row in rows if row['event'] == 'bootstrap']
    assert starts, 'private interpreter did not load delivery-only binding'
    expected = ['runtime_native_scope.require_native_binding'] if enabled else []
    assert all(row['replacements'] == expected and row['replacement_count'] == len(expected)
               for row in starts), 'delivery binding replaced another product function'


def assert_real_delivery_gates(rows, install):
    calls = {(row['file'], row['name']) for row in rows if row['event'] == 'real-call'}
    # Literal names/paths are independent of the adapter's reported outcome.
    required = {('runtime_intent_store.py', 'read_owner'),
                ('runtime_intent_store.py', 'checked_intent'),
                ('runtime_launch_intent.py', 'materialize_launch_intent'),
                ('local_model_auth.py', 'check_configuration_evidence'),
                ('local_model_auth.py', 'source_evidence'),
                ('runtime_local_launch.py', 'inspect_local_launch'),
                ('runtime_delivery.py', 'codex_trust'),
                ('codex_plugin_trust.py', 'verify_approved_plan'),
                ('runtime_delivery.py', 'execute'), ('runtime_delivery.py', 'exec_provider')}
    missing = sorted(name+':'+function for name,function in required
                     if (str(install/'bin/lib'/name),function) not in calls)
    assert not missing, 'R3_REAL_DELIVERY_GATE_UNREACHED: '+','.join(missing)
