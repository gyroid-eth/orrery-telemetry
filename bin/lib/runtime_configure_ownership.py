"""Private explicit-configure outputs in the existing install-state manifest.

No credential acquisition or implicit adoption. Producers pass only admitted,
fixed output roots; this is not a caller/HTTP arbitrary-file writing API. The
existing instruction mutex namespace serializes manifest publishers. A pending
record in that same manifest makes interrupted output publication resumable.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat

from local_model_auth import LocalModelError, canonical_digest, private_directory, secure_read
from runtime_intent_store import ensure_directory

KINDS = {'auth-runner', 'auth-dependency', 'provider-settings', 'provider-instructions', 'skill-link'}
CONFLICT = 'LOCAL_MODEL_CONFIGURE_OWNERSHIP_CONFLICT'
CHANGED = 'LOCAL_MODEL_CONFIGURE_PLAN_CHANGED'


def _path(value):
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts
            or isinstance(value,str) and str(path) != value):
        raise LocalModelError('LOCAL_MODEL_CONFIGURE_OUTPUT_INVALID')
    return path


def _manifest(root):
    root = _path(root)
    try:
        raw = secure_read(root/'install-state.json', os.getuid(), limit=16777216)
        row = json.loads(raw)
        if (not isinstance(row, dict) or row.get('install_dir') != str(root)
                or row.get('schema_version') != 1):
            raise ValueError()
        for key in ('owned_files','owned_dirs'):
            if not isinstance(row.get(key,[]),list) or any(not isinstance(path,str) for path in row.get(key,[])):
                raise ValueError()
        if not isinstance(row.get('configure_owned',{}),dict) or not isinstance(row.get('skill_links',[]),list):
            raise ValueError()
        if any(not isinstance(link,dict) or not all(isinstance(link.get(key),str)
               for key in ('path','target')) for link in row.get('skill_links',[])):
            raise ValueError()
        if not isinstance(row.get('configure_owned_dirs',{}),dict):
            raise ValueError()
        if row.get('configure_pending') is not None and not isinstance(row['configure_pending'],dict):
            raise ValueError()
        return row, raw
    except (OSError, ValueError, TypeError):
        raise LocalModelError('LOCAL_MODEL_INSTALL_MANIFEST_REQUIRED') from None


def _installer_manifest(root):
    """Only R3 configure claims require R3's full manifest schema.

    Existing installations without these claims keep the installer's legacy
    migration/preflight behavior. Explicit local configure still uses _manifest
    and cannot adopt a minimal or unrelated manifest as ownership evidence.
    """
    try:
        raw = secure_read(root/'install-state.json',os.getuid(),limit=16777216)
        row = json.loads(raw)
        if not isinstance(row,dict):raise ValueError()
        if any(key.startswith('configure_') for key in row):return _manifest(root)
        return row,raw
    except (OSError,ValueError,TypeError):
        raise LocalModelError('LOCAL_MODEL_INSTALL_MANIFEST_REQUIRED') from None


def _inventory_mutexes(root, row, paths):
    """Record only the exact lock artifacts returned by the existing engine."""
    files = set(row.get('owned_files',[]))
    directories = set(row.get('owned_dirs',[]))
    for path in paths:
        path = Path(path)
        if not path.is_relative_to(root):raise LocalModelError(CHANGED)
        files.add(str(path))
        parent = path.parent
        while True:
            directories.add(str(parent))
            if parent == root:break
            parent = parent.parent
    return {**row,'owned_files':sorted(files),'owned_dirs':sorted(directories)}


def _observe(path, kind):
    """An exact deletion/adoption fingerprint, never a path-only ownership claim."""
    path = _path(path)
    try:
        if not path.exists() and not path.is_symlink():
            return None
        directory = private_directory(path.parent, os.getuid())
        try:
            info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError()
            result = {'kind':kind, 'uid':info.st_uid}
            if kind == 'skill-link':
                if not stat.S_ISLNK(info.st_mode):
                    raise ValueError()
                result['target'] = os.readlink(path.name, dir_fd=directory)
            else:
                result['mode'] = stat.S_IMODE(info.st_mode)
                result['sha256'] = hashlib.sha256(secure_read(path, os.getuid())).hexdigest()
            return result
        finally:
            os.close(directory)
    except (OSError, ValueError, TypeError):
        raise LocalModelError(CONFLICT) from None


def _desired(output):
    if not isinstance(output, dict) or output.get('kind') not in KINDS:
        raise LocalModelError('LOCAL_MODEL_CONFIGURE_OUTPUT_INVALID')
    result = {'kind':output['kind'], 'uid':os.getuid()}
    if output['kind'] == 'skill-link':
        if set(output) != {'path','kind','target'}:
            raise LocalModelError('LOCAL_MODEL_CONFIGURE_OUTPUT_INVALID')
        target = _path(output['target'])
        # A producer records the approved canonical source, not a mutable link
        # chain or a target selected from the child's HOME during launch.
        try:
            descriptor = private_directory(target, os.getuid())
            os.close(descriptor)
        except (OSError,ValueError):
            raise LocalModelError('LOCAL_MODEL_CONFIGURE_OUTPUT_INVALID') from None
        # Link permissions differ by OS/umask and do not constrain following
        # the link. Ownership uses exact link kind, UID and recorded target.
        result.update(target=str(target))
    else:
        if (set(output) != {'path','kind','mode','content'}
                or type(output['mode']) is not int or output['mode'] not in (0o500,0o600)
                or not isinstance(output['content'], str)):
            raise LocalModelError('LOCAL_MODEL_CONFIGURE_OUTPUT_INVALID')
        result.update(mode=output['mode'], sha256=hashlib.sha256(output['content'].encode()).hexdigest())
    return result


def _missing_parents(path):
    missing = []
    parent = path.parent
    while not parent.exists() and not parent.is_symlink():
        missing.append(str(parent))
        parent = parent.parent
    descriptor = private_directory(parent, os.getuid())
    os.close(descriptor)
    return missing


def plan_outputs(root, outputs, *, allowed_roots):
    """Read-only plan. Existing files must already have matching ownership proof."""
    root = _path(root)
    saved, _ = _manifest(root)
    if saved.get('configure_pending'):
        raise LocalModelError('LOCAL_MODEL_CONFIGURE_PENDING_CONFLICT')
    if inspect_owned_outputs(root)['protected']:
        raise LocalModelError(CONFLICT)
    roots = [_path(path) for path in allowed_roots]
    if not roots or not isinstance(outputs, list) or len(outputs) > 256:
        raise LocalModelError('LOCAL_MODEL_CONFIGURE_OUTPUT_INVALID')
    planned, directories, paths = [], set(), set()
    owned = saved.get('configure_owned', {})
    for output in outputs:
        desired = _desired(output)
        path = _path(output['path'])
        if str(path) in paths or not any(path != base and path.is_relative_to(base) for base in roots):
            raise LocalModelError('LOCAL_MODEL_CONFIGURE_OUTPUT_INVALID')
        paths.add(str(path))
        directories.update(_missing_parents(path))
        before = _observe(path, output['kind'])
        if before is not None and (before != owned.get(str(path)) or str(path) not in saved.get('owned_files', [])):
            raise LocalModelError(CONFLICT)
        planned.append({**deepcopy(output), 'before':before, 'after':desired})
    value = {'kind':'orrery-configure-output-plan-v1','install_dir':str(root),
             'manifest_digest':canonical_digest(saved), 'allowed_roots':list(map(str,roots)),
             'outputs':planned, 'directories':sorted(directories)}
    value['plan_id'] = canonical_digest(value)
    return value


def _publish(path, content, mode, *, expected, kind, parent_mode=0o700):
    """CAS output bytes using a pinned private parent and descriptor rename."""
    path = Path(path)
    ensure_directory(path.parent)
    directory = private_directory(path.parent, os.getuid(), exact_mode=parent_mode)
    temp = '.orrery-configure-'+secrets.token_hex(16)
    try:
        if kind == 'skill-link':
            os.symlink(content, temp, dir_fd=directory)
        else:
            fd = os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode,dir_fd=directory)
            with os.fdopen(fd,'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fchmod(stream.fileno(),mode)
                os.fsync(stream.fileno())
        actual = _observe(path,kind)
        if actual != expected:
            raise LocalModelError(CONFLICT)
        current = path.parent.lstat()
        opened = os.fstat(directory)
        if (current.st_dev,current.st_ino) != (opened.st_dev,opened.st_ino):
            raise LocalModelError(CONFLICT)
        os.replace(temp,path.name,src_dir_fd=directory,dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:os.unlink(temp,dir_fd=directory)
        except FileNotFoundError:pass
        os.close(directory)


def _save_manifest(root, row, previous):
    path = root/'install-state.json'
    # Manifest uses the same output publication checks; no new config ledger.
    before = _observe(path,'provider-settings')
    if secure_read(path,os.getuid(),limit=16777216) != previous:
        raise LocalModelError(CHANGED)
    _publish(path,json.dumps(row,sort_keys=True,ensure_ascii=False,allow_nan=False).encode(),
             0o600,expected=before,kind='provider-settings',parent_mode=None)


def _directory_proof(path):
    try:
        descriptor = private_directory(path,os.getuid(),exact_mode=0o700)
    except (OSError,ValueError):
        raise LocalModelError(CONFLICT) from None
    try:
        info = os.fstat(descriptor)
        return {'dev':info.st_dev,'ino':info.st_ino,'uid':info.st_uid,'mode':stat.S_IMODE(info.st_mode)}
    finally:
        os.close(descriptor)


def apply_outputs(root, plan, *, approved_id, checkpoint=None):
    """Explicit same-plan apply/resume; never called from ordinary launch."""
    root = _path(root)
    if not approved_id:
        raise LocalModelError('LOCAL_MODEL_CONFIGURE_CONFIRMATION_REQUIRED')
    if (plan.get('kind') != 'orrery-configure-output-plan-v1'
            or plan.get('install_dir') != str(root) or plan.get('plan_id') != approved_id
            or canonical_digest({k:v for k,v in plan.items() if k != 'plan_id'}) != approved_id):
        raise LocalModelError(CHANGED)
    try:
        roots = [_path(path) for path in plan['allowed_roots']]
        seen = set()
        if not roots or not isinstance(plan['outputs'],list) or len(plan['outputs']) > 256:
            raise ValueError()
        for output in plan['outputs']:
            path = _path(output['path'])
            if path in seen or not any(path != base and path.is_relative_to(base) for base in roots):
                raise ValueError()
            seen.add(path)
            original = {key:value for key,value in output.items() if key not in ('before','after')}
            if _desired(original) != output['after']:
                raise ValueError()
        directories = [_path(path) for path in plan['directories']]
        if len(set(directories)) != len(directories) or any(
                path == root or not any(path in output.parents for output in seen) for path in directories):
            raise ValueError()
    except (KeyError,TypeError,ValueError,LocalModelError):
        raise LocalModelError(CHANGED) from None

    completed,_ = _manifest(root)
    if completed.get('configure_completed_plan') == approved_id:
        if any(completed.get('configure_owned',{}).get(output['path']) != output['after']
               or output['path'] not in completed.get('owned_files',[])
               or _observe(output['path'],output['kind']) != output['after'] for output in plan['outputs']):
            raise LocalModelError(CONFLICT)
        return {'ok':True,'changed':False,'plan_id':approved_id}

    def admitted():
        row, raw = _manifest(root)
        pending = row.get('configure_pending')
        base = {k:v for k,v in row.items() if k != 'configure_pending'}
        if canonical_digest(base) != plan['manifest_digest']:
            raise LocalModelError(CHANGED)
        if pending is not None and pending.get('plan_id') != approved_id:
            raise LocalModelError('LOCAL_MODEL_CONFIGURE_PENDING_CONFLICT')
        for output in plan['outputs']:
            current = _observe(output['path'],output['kind'])
            allowed = [output['before']]
            if pending is not None:allowed.append(output['after'])
            if current not in allowed:
                raise LocalModelError(CONFLICT)
        recorded_dirs = pending.get('created_dirs',{}) if pending is not None else {}
        if not isinstance(recorded_dirs,dict) or set(recorded_dirs) - set(plan['directories']):
            raise LocalModelError(CONFLICT)
        for name,proof in row.get('configure_owned_dirs',{}).items():
            directory = _path(name)
            if directory.exists() or directory.is_symlink():
                if _directory_proof(directory) != recorded_dirs.get(name,proof):
                    raise LocalModelError(CONFLICT)
        for path in plan['directories']:
            directory = Path(path)
            if directory.exists() or directory.is_symlink():
                if path not in recorded_dirs or _directory_proof(directory) != recorded_dirs[path]:
                    raise LocalModelError(CONFLICT)
            elif path in recorded_dirs:
                raise LocalModelError(CONFLICT)
        return row, raw

    saved, _ = admitted()  # No new mutex/directory on rejected input.
    if (not saved.get('configure_pending') and all(output['before'] == output['after'] for output in plan['outputs'])):
        return {'ok':True,'changed':False,'plan_id':approved_id}
    from instruction_blocks import mutexes
    with mutexes(root,[root/'install-state.json']) as locked_paths:
        saved, raw = admitted()
        expected_pending = {'plan_id':approved_id,'outputs':[{k:out[k] for k in ('path','before','after')}
                             for out in plan['outputs']], 'directories':plan['directories']}
        pending = {**expected_pending,'created_dirs':{}}
        if not saved.get('configure_pending'):
            saved = {**saved,'configure_pending':pending}
            _save_manifest(root,saved,raw)
        else:
            pending = saved['configure_pending']
            if {k:v for k,v in pending.items() if k != 'created_dirs'} != expected_pending:
                raise LocalModelError('LOCAL_MODEL_CONFIGURE_PENDING_CONFLICT')
        if checkpoint:checkpoint('planned')
        for name in sorted(plan['directories'],key=lambda value:len(Path(value).parts)):
            directory = Path(name)
            if name in pending['created_dirs']:
                continue
            parent = private_directory(directory.parent,os.getuid())
            try:
                try:os.mkdir(directory.name,0o700,dir_fd=parent)
                except FileExistsError:raise LocalModelError(CONFLICT) from None
                os.fsync(parent)
            finally:
                os.close(parent)
            if checkpoint:checkpoint('directory-created')
            # A crash before recording the inode leaves an unproven directory;
            # retry refuses it rather than silently adopting somebody's path.
            latest,raw = _manifest(root)
            if latest != saved:
                raise LocalModelError(CHANGED)
            pending = deepcopy(pending)
            pending['created_dirs'][name] = _directory_proof(directory)
            saved = {**saved,'configure_pending':pending}
            _save_manifest(root,saved,raw)
        for output in plan['outputs']:
            admitted()
            if _observe(output['path'],output['kind']) != output['after']:
                content = output['target'] if output['kind'] == 'skill-link' else output['content'].encode()
                _publish(output['path'],content,output['after'].get('mode',0o777),
                         expected=output['before'],kind=output['kind'],
                         parent_mode=None if output['kind']=='skill-link' else 0o700)
            if checkpoint:checkpoint('output-written')
        latest, raw = admitted()
        final = {k:v for k,v in latest.items() if k != 'configure_pending'}
        final['configure_owned'] = {**final.get('configure_owned',{}),
                                   **{out['path']:out['after'] for out in plan['outputs']}}
        final['owned_files'] = sorted(set(final.get('owned_files',[]))|{out['path'] for out in plan['outputs']})
        final['owned_dirs'] = sorted(set(final.get('owned_dirs',[]))|set(pending['created_dirs']))
        final['configure_owned_dirs'] = {**final.get('configure_owned_dirs',{}),**pending['created_dirs']}
        final['configure_completed_plan'] = approved_id
        links = {entry['path']:entry['target'] for entry in final.get('skill_links',[])}
        links.update({out['path']:out['target'] for out in plan['outputs'] if out['kind']=='skill-link'})
        final['skill_links'] = [{'path':path,'target':target} for path,target in sorted(links.items())]
        final = _inventory_mutexes(root,final,locked_paths)
        _save_manifest(root,final,raw)
        if checkpoint:checkpoint('complete')
    return {'ok':True,'changed':True,'plan_id':approved_id}


def inspect_owned_outputs(root):
    """Read-only removal admission, including protected paths for ancestor purge.

    The uninstaller must not recursively delete a parent containing one of the
    protected outputs. Missing artifacts need no deletion; changed/unsafe ones
    retain both their bytes and their manifest ownership claim for manual repair.
    """
    root = _path(root)
    row, _ = _manifest(root)
    if row.get('configure_pending'):
        raise LocalModelError('LOCAL_MODEL_CONFIGURE_PENDING_CONFLICT')
    removable, protected = [], []
    for name, proof in row.get('configure_owned',{}).items():
        try:
            path = _path(name)
            if (not isinstance(proof,dict) or proof.get('kind') not in KINDS
                    or name not in row.get('owned_files',[])):
                raise ValueError()
            actual = _observe(path,proof['kind'])
            if actual is None:
                continue
            if actual != proof:
                raise ValueError()
            removable.append(name)
        except (LocalModelError,ValueError,TypeError,OSError):
            protected.append(name)
    for name,proof in row.get('configure_owned_dirs',{}).items():
        try:
            path = _path(name)
            if not path.exists() and not path.is_symlink():continue
            if name not in row.get('owned_dirs',[]) or _directory_proof(path) != proof:
                raise ValueError()
        except (LocalModelError,ValueError,TypeError,OSError):
            protected.append(name)
    return {'removable':sorted(removable),'protected':sorted(set(protected))}


def installation_preflight(root):
    """Before any installer effect: never overwrite or forget configure proof."""
    root = _path(root)
    path = root/'install-state.json'
    if not path.exists() and not path.is_symlink():return
    row,_ = _installer_manifest(root)
    if not any(key.startswith('configure_') for key in row):return
    result = inspect_owned_outputs(root)
    if result['protected']:
        raise LocalModelError(CONFLICT)


def publish_install_manifest(root, incoming):
    """Installer publisher shares the configure/instruction manifest mutex.

    Incoming payload inventory never silently adopts previously configured
    files, and it cannot discard their exact ownership or pending evidence.
    """
    root = _path(root)
    if (not isinstance(incoming,dict) or incoming.get('install_dir') != str(root)
            or incoming.get('schema_version') != 1
            or any(key.startswith('configure_') for key in incoming)):
        raise LocalModelError(CHANGED)
    path = root/'install-state.json'
    from instruction_blocks import mutexes
    with mutexes(root,[path]) as locked_paths:
        incoming = _inventory_mutexes(root,incoming,locked_paths)
        if path.exists() or path.is_symlink():
            installation_preflight(root)
            prior, raw = _installer_manifest(root)
            final = deepcopy(incoming)
            final.update({key:deepcopy(value) for key,value in prior.items()
                          if key.startswith('configure_')})
            for key in ('owned_files','owned_dirs'):
                retained = (prior.get('configure_owned',{}) if key == 'owned_files'
                            else prior.get('configure_owned_dirs',{}))
                final[key] = sorted(set(final.get(key,[])) | set(retained))
            links = deepcopy(final.get('skill_links',[]))
            targets = {entry['path']:entry['target'] for entry in links}
            for name,proof in prior.get('configure_owned',{}).items():
                if proof.get('kind') != 'skill-link':continue
                if name in targets and targets[name] != proof['target']:
                    raise LocalModelError(CONFLICT)
                if name not in targets:
                    links.append({'path':name,'target':proof['target']})
                    targets[name] = proof['target']
            final['skill_links'] = links
            _save_manifest(root,final,raw)
        else:
            # A fresh install has no prior configure claim. The private writer
            # publishes its first manifest without an untracked temporary file.
            _publish(path,json.dumps(incoming,sort_keys=True,ensure_ascii=False,allow_nan=False).encode(),
                     0o600,expected=None,kind='provider-settings',parent_mode=None)
