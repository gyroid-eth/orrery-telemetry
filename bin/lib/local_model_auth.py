"""Bounded, secret-free command-backed auth core. No launcher or deployment.

The standalone runner reads an existing approved intent owner; it never writes
one, registers an agent, changes trust, or puts credentials in an environment.
Only stdlib imports: the deployed runner can be a single reviewed source file.
"""
from __future__ import annotations

import sys

# The executable must be deployed with an approved absolute Python -I
# shebang. Refuse a manually overridden non-isolated interpreter before
# importing non-builtin modules or fetching any credential.
if __name__ == "__main__" and not sys.flags.isolated:
    sys.stderr.write("LOCAL_MODEL_AUTH_FAILED\n")
    raise SystemExit(1)

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time
import unicodedata

CONTRACT_VERSION = 1
AUTH_FAILED = "LOCAL_MODEL_AUTH_FAILED"


class LocalModelError(RuntimeError):
    """Public errors contain a fixed reason, never command output or paths."""


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def plain_text(value):
    return isinstance(value, str) and bool(value) and not any(
        unicodedata.category(char).startswith("C") for char in value)


def _comparison_path(value):
    return (plain_text(value) and Path(value).is_absolute() and '..' not in Path(value).parts
            and str(Path(value)) == value)


def _comparison_json(value):
    """Reject non-JSON shapes rather than coerce keys, tuples or subclasses."""
    if value is None or type(value) in (str,bool,int,float):return
    if type(value) is list:
        for item in value:_comparison_json(item)
    elif type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():_comparison_json(item)
    else:raise ValueError()


def _comparison_cli(cli):
    """Validate observation structure, NOT observe or authorize an executable."""
    if (not isinstance(cli,dict) or set(cli) != {'path','uid','mode','sha256','version','origin'}
            or not _comparison_path(cli['path']) or not plain_text(cli['version'])
            or type(cli['uid']) is not int or cli['uid'] < 0
            or type(cli['mode']) is not int or not 0 <= cli['mode'] <= 0o777
            or cli['mode'] & 0o022 or not cli['mode'] & 0o100
            or not isinstance(cli['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',cli['sha256'])):
        raise LocalModelError('LOCAL_MODEL_CLI_CHANGED')
    origin = cli['origin']
    if (not isinstance(origin,dict) or set(origin) != {'path','uid'}
            or not _comparison_path(origin['path']) or type(origin['uid']) is not int
            or origin['uid'] < 0
            or not Path(cli['path']).is_relative_to(origin['path'])):
        raise LocalModelError('LOCAL_MODEL_CLI_CHANGED')


def _comparison_scope(scope, version):
    """Separate version provenance; preserve all native effects and ordering.

    Inputs come from the common native producer. This function does not infer
    effective permissions, parse shell rules or turn a proposal into approval.
    """
    invalid = 'LOCAL_MODEL_PERMISSION_PLAN_UNSATISFIABLE'
    if not isinstance(scope,dict) or scope.get('cli_version') != version:
        raise LocalModelError(invalid)
    scope = {key:value for key,value in scope.items() if key != 'cli_version'}
    if 'native_proposal' in scope:
        if not isinstance(scope['native_proposal'],dict):raise LocalModelError(invalid)
        proposal = dict(scope['native_proposal'])
        digest = proposal.pop('plan_digest',None)
        if (digest != canonical_digest(proposal) or proposal.get('declared_cli_version') != version
                or not isinstance(proposal.get('requested_scope'),dict)
                or proposal['requested_scope'].get('cli_version') != version):
            raise LocalModelError(invalid)
        proposal.pop('declared_cli_version')
        proposal['requested_scope'] = {key:value for key,value in proposal['requested_scope'].items()
                                        if key != 'cli_version'}
        scope['native_proposal'] = proposal
    return scope


def compare_cli_configurations(approved, observed):
    """Pure version compatibility proof, NOT native admission or a new intent.

    Neither snapshot is discovered here. The adopted native producer must
    provide complete normalized scope/source/derivation observations, and the
    existing native gate remains necessary. No CLI, credential or FS I/O runs.
    The portable auth runner shares these exact comparison/digest rules.
    """
    try:
        # Fresh JSON values prevent aliases/writes and reject nonfinite data.
        for value in (approved,observed):_comparison_json(value)
        approved, observed = (json.loads(json.dumps(value,allow_nan=False))
                              for value in (approved,observed))
        fields = {'provider','cli','effective_scope','resources','derivation'}
        for value in (approved,observed):
            if (not isinstance(value,dict) or set(value) != fields
                    or value['provider'] not in ('codex','claude')
                    or not isinstance(value['resources'],dict) or not value['resources']
                    or not isinstance(value['derivation'],dict)
                    or set(value['derivation']) != {'model','endpoint','auth','context'}):
                raise ValueError()
            _comparison_cli(value['cli'])
        old, new = approved['cli'], observed['cli']
        changed = old['version'] != new['version']
        bytes_changed = old['sha256'] != new['sha256']
        if (approved['provider'] != observed['provider']
                or any(old[key] != new[key] for key in ('path','uid','origin'))
                or (bytes_changed and not changed)):
            raise LocalModelError('LOCAL_MODEL_CLI_CHANGED')
        before = _comparison_scope(approved['effective_scope'],old['version'])
        after = _comparison_scope(observed['effective_scope'],new['version'])
        if canonical_digest(before) != canonical_digest(after):
            raise LocalModelError('LOCAL_MODEL_PERMISSION_PLAN_UNSATISFIABLE')
        if canonical_digest(approved['resources']) != canonical_digest(observed['resources']):
            raise LocalModelError(AUTH_FAILED)
        if canonical_digest(approved['derivation']) != canonical_digest(observed['derivation']):
            raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        result = {'schema_version':1,'provider':approved['provider'],
            'approved_cli':old,'observed_cli':new,'version_changed':changed,'bytes_changed':bytes_changed,
            'scope_sha256':canonical_digest(before),'resources_sha256':canonical_digest(approved['resources']),
            'derivation_sha256':canonical_digest(approved['derivation'])}
        return {**result,'proof_sha256':canonical_digest(result)}
    except LocalModelError:
        raise
    except (KeyError,TypeError,ValueError,OverflowError,RecursionError):
        raise LocalModelError('LOCAL_MODEL_CLI_CHANGED') from None


def private_directory(path, expected_uid, *, exact_mode=None):
    """Walk with dirfds and NOFOLLOW, allowing only root-owned sticky /tmp.

    Caller owns the returned descriptor. Never resolve a symlink and then
    accidentally validate the substituted target instead of the requested path.
    """
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            writable = stat.S_IMODE(info.st_mode) & 0o022
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in (0, expected_uid) or (writable and not sticky_root):
                raise ValueError("unsafe ancestor")
        info = os.fstat(fd)
        if exact_mode is not None and (info.st_uid != expected_uid or
                                      stat.S_IMODE(info.st_mode) != exact_mode):
            raise ValueError("unsafe directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def secure_read(path, expected_uid, *, mode=None, parent_mode=None, limit=1048576):
    path = Path(path)
    directory = private_directory(path.parent, expected_uid, exact_mode=parent_mode)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                    before.st_uid != expected_uid or before.st_size > limit or
                    stat.S_IMODE(before.st_mode) & 0o022 or
                    (mode is not None and stat.S_IMODE(before.st_mode) != mode)):
                raise ValueError("unsafe file")
            chunks, size = [], 0
            while chunk := os.read(fd, min(65536, limit + 1 - size)):
                size += len(chunk)
                if size > limit:
                    raise ValueError("oversized file")
                chunks.append(chunk)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("file changed during read")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def validate_auth_context(plan):
    if not isinstance(plan, dict):
        raise LocalModelError(AUTH_FAILED)
    env = plan.get("env")
    if (not isinstance(env, dict) or set(env) != {"PATH", "LANG", "HOME", "USER", "LOGNAME"} or
            env["PATH"] != "/usr/bin:/bin" or env["LANG"] != "C" or
            not all(plain_text(value) for value in env.values()) or
            not Path(env["HOME"]).is_absolute() or not plain_text(plan.get("cwd")) or
            not Path(plan["cwd"]).is_absolute() or
            ".." in Path(env["HOME"]).parts or ".." in Path(plan["cwd"]).parts or
            any(type(plan.get(key)) is not int for key in
                ("timeout_ms", "stdout_limit_bytes", "refresh_interval_ms")) or
            plan.get("timeout_ms") != 5000 or plan.get("stdout_limit_bytes") != 8192 or
            plan.get("refresh_interval_ms") != 300000):
        raise LocalModelError(AUTH_FAILED)


def validate_runner(plan, *, observed_digest, observed_contract_version):
    if (type(observed_contract_version) is not int or type(plan.get("runner_contract_version")) is not int or
            observed_contract_version != CONTRACT_VERSION or
            plan.get("runner_contract_version") != observed_contract_version or
            plan.get("runner_digest") != observed_digest):
        raise LocalModelError(AUTH_FAILED)


def executable_evidence(path):
    """Read-only pin of an explicitly approved executable, never a version probe.

    System-owned interpreters are valid in operator-owned safe directories;
    parent-directory ownership and executable ownership are checked separately.
    """
    path = Path(path)
    if (not plain_text(str(path)) or not path.is_absolute() or '..' in path.parts
            or path.resolve() != path):
        raise LocalModelError(AUTH_FAILED)
    directory = private_directory(path.parent,os.getuid())
    try:
        fd = os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid not in (0,os.getuid()) or info.st_mode&0o022
                    or not info.st_mode&0o100 or info.st_size > 536870912):
                raise ValueError('unsafe executable')
            digest = hashlib.sha256()
            size = 0
            while chunk := os.read(fd,65536):
                size += len(chunk)
                if size > 536870912:raise ValueError('oversized executable')
                digest.update(chunk)
            after = os.fstat(fd)
            if (info.st_size,info.st_mtime_ns,info.st_ctime_ns) != (
                    after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise ValueError('executable changed')
            return {'path':str(path),'uid':info.st_uid,'mode':stat.S_IMODE(info.st_mode),
                    'sha256':digest.hexdigest()}
        finally:os.close(fd)
    except (OSError,ValueError):
        raise LocalModelError(AUTH_FAILED) from None
    finally:os.close(directory)


def check_runner_resources(runner, expected_uid):
    try:
        if (not isinstance(runner, dict) or not isinstance(runner.get("dependency_digests"), dict) or
                not re.fullmatch(r"[0-9a-f]{64}", runner.get("sha256", ""))):
            raise ValueError("invalid runner reference")
        data = secure_read(runner["path"], expected_uid, mode=0o500, parent_mode=0o700)
        validate_runner({"runner_digest": runner["sha256"],
                         "runner_contract_version": runner["contract_version"]},
                        observed_digest=hashlib.sha256(data).hexdigest(),
                        observed_contract_version=CONTRACT_VERSION)
        if 'python' in runner and executable_evidence(runner['python']['path']) != runner['python']:
            raise ValueError('interpreter changed')
        for path, digest in runner["dependency_digests"].items():
            if not isinstance(digest, str) or hashlib.sha256(secure_read(path, expected_uid)).hexdigest() != digest:
                raise ValueError("dependency changed")
    except (KeyError, TypeError, OSError, ValueError):
        raise LocalModelError(AUTH_FAILED) from None


def check_auth_source(source, argv, expected_uid):
    """Recheck explicit configure evidence without executing the credential CLI."""
    try:
        if (not isinstance(source,dict) or set(source) != {'executable','dependency_digests'}
                or source['executable']['path'] != argv[0]
                or executable_evidence(argv[0]) != source['executable']
                or not isinstance(source['dependency_digests'],dict)):
            raise ValueError('credential source changed')
        for path,digest in source['dependency_digests'].items():
            if hashlib.sha256(secure_read(path,expected_uid)).hexdigest() != digest:
                raise ValueError('credential dependency changed')
    except (KeyError,IndexError,TypeError,OSError,ValueError):
        raise LocalModelError(AUTH_FAILED) from None


def source_evidence(paths):
    """Read-only fingerprints of explicit settings/resource paths, including absence.

    This does not discover configuration sources or interpret native policy.
    The provider configure plan must supply the complete approved source set.
    """
    if (not isinstance(paths,list) or len(paths)>4096 or any(not isinstance(path,str) for path in paths)
            or len(set(paths)) != len(paths)):raise LocalModelError(AUTH_FAILED)
    result = {}
    try:
        for value in paths:
            path = Path(value)
            if (not path.is_absolute() or '..' in path.parts or str(path) != value
                    or path.resolve() != path):raise ValueError()
            parent = path.parent
            while not parent.exists() and not parent.is_symlink():parent = parent.parent
            directory = private_directory(parent,os.getuid())
            os.close(directory)
            if not path.exists() and not path.is_symlink():
                result[value] = {'state':'absent'}
                continue
            directory = private_directory(path.parent,os.getuid())
            try:fd = os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            finally:os.close(directory)
            try:
                before = os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or before.st_uid not in (0,os.getuid()) or before.st_mode&0o022
                        or before.st_size>1048576):raise ValueError()
                digest,size = hashlib.sha256(),0
                while chunk := os.read(fd,65536):
                    size += len(chunk)
                    if size>1048576:raise ValueError()
                    digest.update(chunk)
                after = os.fstat(fd)
                if (before.st_ino,before.st_dev,before.st_uid,before.st_mode,before.st_size,
                        before.st_mtime_ns,before.st_ctime_ns) != (
                        after.st_ino,after.st_dev,after.st_uid,after.st_mode,after.st_size,
                        after.st_mtime_ns,after.st_ctime_ns):raise ValueError()
                result[value] = {'state':'present','uid':before.st_uid,
                                 'mode':stat.S_IMODE(before.st_mode),'sha256':digest.hexdigest()}
            finally:os.close(fd)
        return result
    except (OSError,ValueError,TypeError):raise LocalModelError(AUTH_FAILED) from None


def skill_source_evidence(sources):
    """Fingerprint declared skill trees; do not claim native visibility/loading."""
    try:
        if not isinstance(sources,list) or len(sources)>256 or len(set(sources)) != len(sources):raise ValueError()
        result = {}
        for value in sources:
            root = Path(value)
            if not root.is_absolute() or str(root) != value or root.resolve() != root:raise ValueError()
            directory = private_directory(root,os.getuid())
            os.close(directory)
            files,count = [],0
            for path in root.rglob('*'):
                count += 1
                if count>4096:raise ValueError()
                if path.is_symlink():raise ValueError()
                if path.is_dir():
                    directory = private_directory(path,os.getuid());os.close(directory)
                else:files.append(str(path))
            evidence = source_evidence(sorted(files))
            if str(root/'SKILL.md') not in evidence:raise ValueError()
            result[value] = {'skill_md_sha256':evidence[str(root/'SKILL.md')]['sha256'],'files':evidence}
        return result
    except (KeyError,OSError,TypeError,ValueError):raise LocalModelError(AUTH_FAILED) from None


def check_configuration_resources(evidence):
    """Strict non-CLI drift and CLI safety; NOT version or native admission."""
    try:
        required={'contract_version','cli','settings_sources','skill_sources','renderer_sources',
                  'requires_native_validation'}
        if (set(evidence) not in (required,required|{'runtime_python'}) or evidence['contract_version'] != 1
                or evidence['requires_native_validation'] is not True):raise ValueError()
        if 'runtime_python' in evidence:
            python=evidence['runtime_python']
            if canonical_digest(executable_evidence(python['path']))!=canonical_digest(python):raise ValueError()
        cli = dict(evidence['cli'])
        if not plain_text(cli.pop('declared_version')):raise ValueError()
        origin = cli.pop('origin',None)
        observed = executable_evidence(cli['path'])
        if set(cli) != {'path','uid','mode','sha256'} or observed['uid'] != cli['uid']:raise ValueError()
        if origin is not None and canonical_digest(cli_installation_origin(cli['path'])) != canonical_digest(origin):
            raise ValueError()
        for key in ('settings_sources','renderer_sources'):
            if source_evidence(list(evidence[key])) != evidence[key]:raise ValueError()
        if skill_source_evidence(list(evidence['skill_sources'])) != evidence['skill_sources']:raise ValueError()
    except (KeyError,TypeError,OSError,ValueError):raise LocalModelError(AUTH_FAILED) from None


def check_configuration_evidence(evidence, *, intent=None, cli_acceptance=None):
    """Common recheck; a comparison proof replaces ONLY old CLI byte equality."""
    check_configuration_resources(evidence)
    if cli_acceptance is not None:
        if intent is None or canonical_digest(intent.get('configuration_evidence')) != canonical_digest(evidence):
            raise LocalModelError(AUTH_FAILED)
        validate_cli_acceptance(intent,cli_acceptance)
        return
    try:
        cli = {key:value for key,value in evidence['cli'].items() if key not in ('declared_version','origin')}
        if canonical_digest(executable_evidence(cli['path'])) != canonical_digest(cli):raise ValueError()
    except (KeyError,TypeError,OSError,ValueError):raise LocalModelError(AUTH_FAILED) from None



def cli_installation_origin(path):
    """Filesystem install origin, not proof of package/native capability."""
    try:
        path = Path(path)
        if path.resolve() != path:raise ValueError()
        fd = private_directory(path.parent,os.getuid())
        try:return {'path':str(path.parent),'uid':os.fstat(fd).st_uid}
        finally:os.close(fd)
    except (OSError,ValueError,RuntimeError):raise LocalModelError(AUTH_FAILED) from None


def cli_configuration_snapshot(intent):
    """Read approved comparison inputs from the sole owner; no discovery."""
    try:
        if intent.get('intent_digest') != intent_digest(intent):
            raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        evidence = intent['configuration_evidence']
        cli = dict(evidence['cli'])
        cli['version'] = cli.pop('declared_version')
        _comparison_cli(cli)
        resolved = intent['model']['resolved']
        auth = resolved['auth']
        value = {'provider':intent['provider'], 'cli':cli,
            'effective_scope':intent['effective_scope'],
            'resources':{'configuration':{k:v for k,v in evidence.items() if k!='cli'},
                'scope_sources':intent['scope_evidence'], 'profile_sha256':intent['model']['profile_sha256'],
                'runner':auth['runner'] if auth is not None else None,
                'auth_source':auth.get('source') if auth is not None else None},
            'derivation':{'model':resolved['model'], 'endpoint':resolved['adapter'],
                'auth':auth['context'] if auth is not None else None,
                'context':{key:resolved[key] for key in ('context_window','max_output_tokens')}}}
        return json.loads(json.dumps(value,allow_nan=False))
    except LocalModelError:
        raise
    except (KeyError,TypeError,ValueError):
        raise LocalModelError('LOCAL_MODEL_CLI_CHANGED') from None


def create_cli_acceptance(intent, observation):
    """Common comparison producer; its output still requires native admission."""
    proof = compare_cli_configurations(cli_configuration_snapshot(intent),observation)
    proof.pop('proof_sha256')
    proof['intent_digest'] = intent['intent_digest']
    return {**proof,'proof_sha256':canonical_digest(proof)}


def validate_cli_acceptance(intent, proof, *, check_current=True):
    """Validate a producer receipt/reference and current bytes, NOT permissions."""
    try:
        _comparison_json(proof)
        approved = cli_configuration_snapshot(intent)
        old,new = approved['cli'],proof['observed_cli']
        _comparison_cli(new)
        version_changed,bytes_changed = old['version']!=new['version'],old['sha256']!=new['sha256']
        if (any(old[key]!=new[key] for key in ('path','uid','origin'))
                or (bytes_changed and not version_changed)):
            raise ValueError()
        expected = {'schema_version':1,'provider':intent['provider'],
            'approved_cli':old,'observed_cli':new,
            'version_changed':version_changed,'bytes_changed':bytes_changed,
            'scope_sha256':canonical_digest(_comparison_scope(approved['effective_scope'],old['version'])),
            'resources_sha256':canonical_digest(approved['resources']),
            'derivation_sha256':canonical_digest(approved['derivation']),
            'intent_digest':intent['intent_digest']}
        expected['proof_sha256'] = canonical_digest(expected)
        if canonical_digest(proof) != canonical_digest(expected):raise ValueError()
        if check_current and (
                canonical_digest(executable_evidence(new['path'])) != canonical_digest(
                    {key:new[key] for key in ('path','uid','mode','sha256')})
                or canonical_digest(cli_installation_origin(new['path'])) != canonical_digest(new['origin'])):
            raise ValueError()
        return json.loads(json.dumps(proof,allow_nan=False))
    except LocalModelError:
        raise
    except (KeyError,TypeError,ValueError,OSError,OverflowError,RecursionError):
        raise LocalModelError('LOCAL_MODEL_CLI_CHANGED') from None


def cli_version_notice(proof):
    """Non-secret result projection: no endpoint/helper/path/permission plan."""
    return {'approved_version':proof['approved_cli']['version'],
        'observed_version':proof['observed_cli']['version'],
        'approved_sha256':proof['approved_cli']['sha256'],
        'observed_sha256':proof['observed_cli']['sha256']}


def _read_cli_launch_auth(owner,intent):
    """Read the fixed active execution receipt, never a second approval file."""
    if 'execution_policy' in intent or 'legacy_context' in intent:
        try:return None,_read_legacy_launch_auth(owner,intent)
        except Exception:raise LocalModelError(AUTH_FAILED) from None
    path = Path(owner).parent/'runtime/launch.json'
    if not path.exists() and not path.is_symlink():return None,None
    row = json.loads(secure_read(path,os.getuid(),mode=0o600,parent_mode=0o700))
    if not isinstance(row,dict):raise LocalModelError(AUTH_FAILED)
    if 'cli_acceptance' not in row:return None,None
    proof = row['cli_acceptance']
    if not isinstance(proof,dict):raise LocalModelError(AUTH_FAILED)
    if Path(owner).name!='profile.json':raise LocalModelError(AUTH_FAILED)
    execution = row.get('execution',{})
    if not isinstance(execution,dict):raise LocalModelError(AUTH_FAILED)
    if (row.get('kind')!='orrery-global-launch-intent-v1'
            or row.get('intent_digest')!=intent['intent_digest']
            or row.get('provider')!=intent['provider'] or row.get('cwd')!=intent['cwd']
            or row.get('phase') not in ('exec-unknown','running')
            or execution.get('outcome')!={'state':'pending','returncode':None}
            or os.environ.get('AGENTSTACK_LAUNCH_ID')!=row.get('launch_id')
            or os.environ.get('AGENTSTACK_LAUNCH_NONCE')!=row.get('receipt_nonce')
            or not plain_text(row.get('launch_id')) or not plain_text(row.get('receipt_nonce'))):
        raise LocalModelError(AUTH_FAILED)
    runner = execution.get('runner',{})
    provider = os.environ.get('AGENTSTACK_PROVIDER_PID','')
    if (not isinstance(runner,dict) or set(runner)!={'pid','process_start'} or type(runner['pid']) is not int
            or runner['pid']<=0 or not plain_text(runner['process_start'])
            or not re.fullmatch('[1-9][0-9]*',provider)):
        raise LocalModelError(AUTH_FAILED)
    return validate_cli_acceptance(intent,proof),row


def _legacy_contract(intent,*,_fixed_root=None):
    """Load the SAME closed readers from fixed, source-pinned bytes.

    The isolated standalone runner cannot discover/import product modules via
    sys.path or trust an existing sys.modules cache. Validate the entire fixed
    dependency set before compiling any of it, and bind its product imports
    only to that set/current auth core. No alternate schema or state authority.
    read_auth_plan calls this only AFTER common runner/source/profile checks.
    """
    import builtins
    from types import SimpleNamespace
    # Auth keeps its existing exact runner layout. The fixed proxy consumer
    # has no auth-runner requirement (keyless profiles still need Mail); only
    # that internal consumer supplies its own physical install root here.
    if _fixed_root is None:
        runner=Path(intent['model']['resolved']['auth']['runner']['path'])
        if (runner.name!='runner' or runner.parent.parent.name!='local-model-auth'
                or runner.parent.parent.parent.name!='libexec'):
            raise LocalModelError(AUTH_FAILED)
        root=runner.parents[3]
    else:
        root=Path(_fixed_root)
        if not root.is_absolute() or root.resolve()!=root:raise LocalModelError(AUTH_FAILED)
    if intent['legacy_context']['install_root']!=str(root):raise LocalModelError(AUTH_FAILED)
    names=('runtime_intent_store','runtime_preview_policy','runtime_legacy_context',
        'runtime_legacy_execution','runtime_legacy_registration',
        'instruction_blocks','local_model_profiles','runtime_provider_projection',
        'runtime_model_resources','runtime_legacy_configuration')
    if _fixed_root is not None:
        names+=('process_observation','runtime_caller_schema','runtime_native_scope','runtime_launch_intent','runtime_local_launch')
    code={};records={}
    for name in names:
        path=root/'bin/lib'/(name+'.py')
        proof=intent['configuration_evidence']['renderer_sources'].get(str(path))
        if type(proof) is not dict or proof.get('state')!='present':raise LocalModelError(AUTH_FAILED)
        content=secure_read(path,os.getuid())
        if hashlib.sha256(content).hexdigest()!=proof.get('sha256'):raise LocalModelError(AUTH_FAILED)
        code[name]=(path,content);records[str(path)]=proof
    modules={'local_model_auth':SimpleNamespace(**globals())}
    original=builtins.__import__
    def fixed_import(name,globals=None,locals=None,fromlist=(),level=0):
        if level:raise LocalModelError(AUTH_FAILED)
        if name in modules:return modules[name]
        if name.startswith(('runtime_','local_model_')):raise LocalModelError(AUTH_FAILED)
        return original(name,globals,locals,fromlist,level)
    for name in names:
        path,content=code[name]
        module=SimpleNamespace(__file__=str(path),__name__=name,
            __builtins__={**vars(builtins),'__import__':fixed_import})
        exec(compile(content,str(path),'exec'),module.__dict__)
        modules[name]=module
    if source_evidence(list(records))!=records:raise LocalModelError(AUTH_FAILED)
    return modules


def _read_legacy_launch_auth(owner,intent):
    """Require active same-owner registration/execution, never missing-row None."""
    api=_legacy_contract(intent)
    # The same reader runs before AND after process observation, including TTL
    # refresh. Source-pinned modules above supply the existing pure renderer;
    # never import an unchecked product cache or rewrite missing output here.
    return api['runtime_legacy_execution'].active_execution_snapshot(owner,intent)['row']


def _validate_auth_provider_process(intent,row):
    """Use only the source-pinned installed shared process observer."""
    runner = Path(intent['model']['resolved']['auth']['runner']['path'])
    if os.environ.get('PATH')!='/usr/bin:/bin':raise LocalModelError(AUTH_FAILED)
    # Same fixed layout produced by explicit runner_output; no PATH/import scan.
    if (runner.name!='runner' or runner.parent.parent.name!='local-model-auth'
            or runner.parent.parent.parent.name!='libexec'):
        raise LocalModelError(AUTH_FAILED)
    script = runner.parents[3]/'bin/lib/process_observation.py'
    record = intent['configuration_evidence']['renderer_sources'].get(str(script))
    if not isinstance(record,dict) or record.get('state')!='present':
        raise LocalModelError(AUTH_FAILED)
    content = secure_read(script,os.getuid())
    if hashlib.sha256(content).hexdigest()!=record.get('sha256'):
        raise LocalModelError(AUTH_FAILED)
    namespace = {'__file__':str(script),'__name__':'orrery_auth_process_observation'}
    exec(compile(content,str(script),'exec'),namespace)
    provider = int(os.environ['AGENTSTACK_PROVIDER_PID'])
    if 'legacy_context' in intent:
        # The shared ancestry gate observes Q twice, but legacy additionally
        # pins Q's originally saved birth, not just the current live PID.
        if not namespace['active_provider_binding'](os.getpid(),row['execution']['provider'],
                row['execution']['runner'],os.getuid()):
            raise LocalModelError(AUTH_FAILED)
    elif not namespace['auth_process_binding'](os.getpid(),provider,row['execution']['runner'],os.getuid()):
        raise LocalModelError(AUTH_FAILED)


def _bounded_process(argv, *, cwd, env, timeout, **_):
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(argv, cwd=cwd, env=env, shell=False, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    complete = False
    try:
        chunks, size = [], 0
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not ready.select(remaining):
                    raise ValueError("credential deadline")
                chunk = os.read(process.stdout.fileno(), 8193 - size)
                if not chunk:
                    ready.unregister(process.stdout)
                    break
                size += len(chunk)
                if size > 8192:
                    raise ValueError("credential output limit")
                chunks.append(chunk)
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        complete = True
        return subprocess.CompletedProcess(argv, code, stdout=b"".join(chunks))
    finally:
        # Also terminate descendants that retained the stdout pipe on timeout.
        if not complete:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.stdout.close()
        process.wait()


def run_auth(plan, *, executor=None):
    try:
        validate_auth_context(plan)
        argv = plan["argv"]
        if (not isinstance(argv, list) or not argv or
                not all(plain_text(arg) for arg in argv) or not Path(argv[0]).is_absolute()):
            raise ValueError("invalid command")
        if 'source' in plan:check_auth_source(plan['source'],argv,os.getuid())
        result = (executor or _bounded_process)(list(argv), cwd=plan["cwd"], env=dict(plan["env"]),
                                               shell=False, timeout=5, stdout=subprocess.PIPE,
                                               stderr=subprocess.DEVNULL)
        raw = result.stdout
        if result.returncode != 0 or not isinstance(raw, bytes) or not 0 < len(raw) <= 8192:
            raise ValueError("invalid credential result")
        token = raw[:-1] if raw.endswith(b"\n") else raw
        text = token.decode("utf-8")
        if not plain_text(text) or not text.strip():
            raise ValueError("invalid credential")
        return token
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, LocalModelError):
        raise LocalModelError(AUTH_FAILED) from None


def intent_digest(intent):
    return canonical_digest({key: value for key, value in intent.items() if key != "intent_digest"})


def read_auth_plan(owner, digest, provider, *, runner_path):
    """Read an existing owner, not a new auth cache or second intent store."""
    try:
        owner_frame = json.loads(secure_read(owner, os.getuid(), mode=0o600, parent_mode=0o700))
        intent = owner_frame["launch_intent"]
        if not isinstance(intent, dict) or not isinstance(intent.get("scope_evidence"), dict):
            raise ValueError("invalid intent")
        if intent_digest(intent) != digest or intent["intent_digest"] != digest or intent["provider"] != provider:
            raise ValueError("intent mismatch")
        resolved = intent["model"]["resolved"]
        legacy='execution_policy' in intent or 'legacy_context' in intent
        # Preview's pinned schema code is loaded only after every common
        # runner/source/profile check below; ordinary global proof order stays.
        proof,row = (None,None) if legacy else _read_cli_launch_auth(owner,intent)
        if 'configuration_evidence' in intent:
            check_configuration_evidence(intent['configuration_evidence'],intent=intent,cli_acceptance=proof)
        auth = resolved["auth"]
        if (intent["operator_uid"] != os.getuid() or auth["context"]["cwd"] != intent["local_models_root"]):
            raise ValueError("auth context mismatch")
        if Path(auth["runner"]["path"]) != Path(runner_path).absolute():
            raise ValueError("runner mismatch")
        check_runner_resources(auth["runner"], os.getuid())
        # Profile drift is checked here too, without importing executable
        # dependencies before validating their digest.
        import tomllib
        root = auth["context"]["cwd"]
        ref = intent["model"]["ref"]
        if not isinstance(ref, str) or not re.fullmatch(r"local:[a-z][a-z0-9_-]{0,47}", ref):
            raise ValueError("invalid profile reference")
        name = ref[6:]
        profile = tomllib.loads(secure_read(Path(root) / (name + ".toml"), os.getuid(),
                                          mode=0o600, parent_mode=0o700, limit=65536).decode())
        if (canonical_digest(profile) != intent["model"]["profile_sha256"] or
                profile[provider] != resolved["adapter"]):
            raise ValueError("profile changed")
        for path, digest in intent.get("scope_evidence", {}).items():
            if hashlib.sha256(secure_read(path, os.getuid())).hexdigest() != digest:
                raise ValueError("settings changed")
        plan = {**auth["context"], "argv": resolved["adapter"]["auth_command"]}
        if 'source' in auth:
            check_auth_source(auth['source'],plan['argv'],os.getuid())
            plan['source'] = auth['source']
        validate_auth_context(plan)
        if legacy:
            if hashlib.sha256(secure_read(intent['task'],os.getuid())).hexdigest()!=intent['task_sha256']:
                raise ValueError('task changed')
            proof,row=_read_cli_launch_auth(owner,intent)
        if row is not None:
            # Compile only the already-pinned observer, after ALL runner/source
            # checks. Re-read receipt and owner after process observation too.
            _validate_auth_provider_process(intent,row)
            current_proof,current_row = _read_cli_launch_auth(owner,intent)
            current_frame = json.loads(secure_read(owner,os.getuid(),mode=0o600,parent_mode=0o700))
            current = current_frame['launch_intent']
            if (canonical_digest(current)!=canonical_digest(intent)
                    or canonical_digest(current_row)!=canonical_digest(row)
                    or canonical_digest(current_proof)!=canonical_digest(proof)):
                raise ValueError('execution changed')
            if legacy:
                if canonical_digest(current_frame)!=canonical_digest(owner_frame):
                    raise ValueError('owner changed')
                # TTL refresh repeats these same checks; no cached permission
                # or synthetic cli_acceptance/native binding is introduced.
                check_configuration_evidence(intent['configuration_evidence'])
                check_runner_resources(auth['runner'],os.getuid())
                if 'source' in auth:check_auth_source(auth['source'],plan['argv'],os.getuid())
                current_profile=tomllib.loads(secure_read(Path(root)/(name+'.toml'),os.getuid(),
                    mode=0o600,parent_mode=0o700,limit=65536).decode())
                if canonical_digest(current_profile)!=intent['model']['profile_sha256']:
                    raise ValueError('profile changed')
                if hashlib.sha256(secure_read(intent['task'],os.getuid())).hexdigest()!=intent['task_sha256']:
                    raise ValueError('task changed')
                for path,digest in intent['scope_evidence'].items():
                    if hashlib.sha256(secure_read(path,os.getuid())).hexdigest()!=digest:
                        raise ValueError('settings changed')
        return plan
    except Exception:
        # The standalone security boundary must not expose a traceback/source
        # detail when pinned observation cannot load or complete. No key has
        # been requested yet, so every such uncertainty is a fixed refusal.
        raise LocalModelError(AUTH_FAILED) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read an approved local model credential")
    parser.add_argument("--intent", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--provider", required=True, choices=("codex", "claude"))
    args = parser.parse_args(argv)
    try:
        plan = read_auth_plan(args.intent, args.digest, args.provider, runner_path=__file__)
        token = run_auth(plan)
    except LocalModelError:
        sys.stderr.write(AUTH_FAILED + "\n")
        return 1
    sys.stdout.buffer.write(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
