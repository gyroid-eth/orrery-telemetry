#!/usr/bin/env python3
"""User instruction blocks: one parser, renderer, migration receipt and detector.

No Mail storage is opened. A plan is read-only; mutations require a matching
plan and consent. Backups contain the user's full file and remain until purge.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from functools import cache
import runpy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

BEGIN = {p: f'<!-- >>> claude-agent-stack (managed: agentstack-{p}-setup) -->'.encode() for p in ('claude', 'codex')}
END = b'<!-- <<< claude-agent-stack -->'
MESSAGES = {
    'identity-unavailable': 'This session is not participating in ORRERY; use agent-start <workdir> (Claude) or agent-start-codex <workdir> (Codex) to start a coordinated session.',
    'identity-binding-unresolved': 'This session has an ORRERY registration but its identity binding is unresolved; ask the operator to run agentstack-doctor and recover the existing identity before retrying.',
}
TERMINAL = {'complete', 'retained', 'uninstalled'}


class Refused(Exception):
    def __init__(self, reason, detail=''):
        self.reason, self.detail = reason, str(detail)
        super().__init__(f'{reason}: {detail}')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def parse(data):
    """Return a single span, including marker line endings; outside bytes survive."""
    offset, start, provider, spans = 0, None, None, []
    for line in data.splitlines(keepends=True):
        token = line.rstrip(b'\r\n')
        # A UTF-8 BOM is outside the span, even when it precedes the first marker.
        bom = 3 if offset == 0 and token.startswith(b'\xef\xbb\xbf') else 0
        token = token[bom:]
        found = next((p for p, b in BEGIN.items() if token == b), None)
        if found:
            if start is not None or spans:
                raise Refused('INSTRUCTION_DETECTION_UNKNOWN', 'malformed: nested/duplicate marker')
            start, provider = offset + bom, found
        elif token == END:
            if start is None:
                raise Refused('INSTRUCTION_DETECTION_UNKNOWN', 'malformed: isolated end marker')
            spans.append((start, offset + len(line), provider)); start = None
        offset += len(line)
    if start is not None:
        raise Refused('INSTRUCTION_DETECTION_UNKNOWN', 'malformed: unclosed marker')
    return spans[0] if spans else None


def block_bytes(data):
    span = parse(data)
    return data[span[0]:span[1]] if span else None


def replace_block(data, block):
    span = parse(data)
    if span:
        return data[:span[0]] + block + data[span[1]:]
    # Do not collapse, strip or rewrite the user's existing bytes.
    return data + (b'\n' if data and not data.endswith(b'\n') else b'') + block


@cache
def render(provider, template, install, project, renderer='literal-v2'):
    if renderer == 'legacy-sed-v1':
        # These are the actual released sed replacement rules (including '&',
        # backslashes and the delimiter). A sed error cannot prove provenance.
        cmd = ['sed', '-e', f's|__AGENTSTACK_HOME__|{install}|g', '-e', f's|__AGENTSTACK_PROJECT_KEY__|{project or "<set AGENTSTACK_PROJECT_KEY>"}|g']
        r = subprocess.run(cmd, input=template, capture_output=True)
        if r.returncode:
            raise Refused('INSTRUCTION_DETECTION_UNKNOWN', 'historical renderer failed')
        body = r.stdout
    else:
        body = template.replace(b'__AGENTSTACK_HOME__', str(install).encode()).replace(b'__AGENTSTACK_PROJECT_KEY__', (project or '<set AGENTSTACK_PROJECT_KEY>').encode())
        for key, text in MESSAGES.items():
            body = body.replace(('__ORRERY_' + key.upper().replace('-', '_') + '__').encode(), text.encode())
    return BEGIN[provider] + b'\n' + body + (b'' if body.endswith(b'\n') else b'\n') + END + b'\n'


def acl(path):
    if sys.platform != 'darwin':
        return b''
    r = subprocess.run(['/bin/ls', '-lde', str(path)], capture_output=True, check=True)
    # First line contains the pathname; subsequent numbered lines are ACL entries.
    return b'\n'.join(line.split(b': ', 1)[1] for line in r.stdout.splitlines()[1:] if b': ' in line)


def attrs(path):
    if hasattr(os, 'listxattr'):
        return {name: base64.b64encode(os.getxattr(path, name)).decode() for name in sorted(os.listxattr(path))}
    # macOS Python builds may omit the Linux os.*xattr API. The system utility
    # preserves binary values via hex; absence/errors are refusals, not data loss.
    names=subprocess.run(['/usr/bin/xattr',str(path)],check=True,capture_output=True,text=True).stdout.splitlines()
    return {name:base64.b64encode(bytes.fromhex(subprocess.run(['/usr/bin/xattr','-px',name,str(path)],check=True,capture_output=True,text=True).stdout)).decode() for name in sorted(names)}


def set_attr(path, name, value):
    data=base64.b64decode(value)
    if hasattr(os, 'setxattr'):os.setxattr(path,name,data)
    else:subprocess.run(['/usr/bin/xattr','-wx',name,data.hex(),str(path)],check=True,capture_output=True)


def has_marker(data):
    return any(marker in (data or b'') for marker in (*BEGIN.values(), END))


def snapshot(path, *, source_candidate=False):
    """Pin every symlink component; never sever a user's symlink on replacement."""
    requested = Path(os.path.abspath(path))
    pending, resolved, chain, hops = list(requested.parts[1:]), Path('/'), [], 0
    while pending:
        part = pending.pop(0)
        if part in ('', '.'):
            continue
        if part == '..':
            resolved = resolved.parent; continue
        current = resolved / part
        try:
            st = current.lstat()
        except FileNotFoundError:
            resolved = current.joinpath(*pending); break
        if stat.S_ISLNK(st.st_mode):
            hops += 1
            if hops > 40 or (st.st_uid != os.getuid() and not (st.st_uid == 0 and str(current) in ('/tmp','/var'))):
                raise Refused('INSTRUCTION_PATH_UNSAFE', current)
            try:
                current.stat()
            except (OSError, RuntimeError):
                raise Refused('INSTRUCTION_PATH_UNSAFE', current)
            text = os.readlink(current)
            chain.append({'path': str(current), 'dev': st.st_dev, 'ino': st.st_ino, 'link_text': text})
            target = Path(text)
            if target.is_absolute():
                resolved = Path('/')
                pending = list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
        else:
            resolved = current
    try:
        st = resolved.stat()
    except FileNotFoundError:
        st = None
    if st:
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise Refused('INSTRUCTION_PATH_UNSAFE', resolved)
        data = resolved.read_bytes()
        # Automatic discovery reads unrelated notes but never selects them for
        # mutation. Explicit paths and targets retain strict link admission.
        if st.st_nlink != 1 and (not source_candidate or has_marker(data)):
            raise Refused('INSTRUCTION_HARDLINK_REQUIRES_MANUAL_COPY', resolved)
        ac, xa = acl(resolved), attrs(resolved)
        metadata = {'dev': st.st_dev, 'ino': st.st_ino, 'mode': stat.S_IMODE(st.st_mode), 'nlink': st.st_nlink, 'acl_sha256': sha(ac), 'xattr_sha256': sha(canonical(xa))}
    else:
        data = None
        metadata = dict.fromkeys(('dev', 'ino', 'mode', 'nlink', 'acl_sha256', 'xattr_sha256'))
    return {'requested_path': str(requested), 'resolved_path': str(resolved), 'link_chain': chain, 'metadata': metadata}, data


def verify(entry, expected, *, completed=False):
    now, data = snapshot(entry['requested_path'])
    if now['resolved_path'] != entry['resolved_path'] or now['link_chain'] != entry['link_chain'] or (sha(data) if data is not None else None) != expected:
        raise Refused('INSTRUCTION_PLAN_CHANGED', entry['requested_path'])
    old, new = entry['metadata'], now['metadata']
    keys = ('mode', 'nlink', 'acl_sha256', 'xattr_sha256') if completed else tuple(old)
    # An originally absent file acquires ordinary private metadata on first write.
    if completed and old['ino'] is None:
        if new['mode'] != 0o600 or new['nlink'] != 1:
            raise Refused('INSTRUCTION_PLAN_CHANGED', entry['requested_path'])
    elif any(old[k] != new[k] for k in keys):
        raise Refused('INSTRUCTION_PLAN_CHANGED', entry['requested_path'])
    return data


def private_dir(path):
    path = Path(path)
    if not path.exists():
        private_dir(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise Refused('INSTRUCTION_PATH_UNSAFE', path)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, data, *, entry=None):
    path = Path(path)
    if entry:
        path.parent.mkdir(parents=True, exist_ok=True)
        old = verify(entry, entry['before_sha256'])
        old_acl = acl(path) if old is not None else b''
        old_attrs = attrs(path) if old is not None else {}
        mode = entry['metadata']['mode'] if old is not None else 0o600
    else:
        private_dir(path.parent); old_acl, old_attrs, mode = b'', {}, 0o600
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_nlink != 1 or path.stat().st_mode & 0o077)):
            raise Refused('INSTRUCTION_PATH_UNSAFE', path)
    fd, name = tempfile.mkstemp(prefix='.orrery-instructions-', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fchmod(out.fileno(), mode); os.fsync(out.fileno())
        for key, value in old_attrs.items():
            set_attr(tmp, key, value)
        if old_acl:
            subprocess.run(['/bin/chmod', '-E', str(tmp)], input=old_acl + b'\n', check=True, capture_output=True)
        if entry and old is not None and (attrs(tmp) != old_attrs or acl(tmp) != old_acl):
            raise Refused('INSTRUCTION_PATH_UNSAFE', 'metadata transfer failed')
        sync_fd=os.open(tmp,os.O_RDONLY)
        try:os.fsync(sync_fd)
        finally:os.close(sync_fd)
        if entry:
            # Same metadata admission, including nlink, immediately before rename.
            verify(entry, entry['before_sha256'])
        os.replace(tmp, path); sync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


@cache
def contract():
    document=json.loads(Path(__file__).with_name('instruction_contract.json').read_bytes())
    def expand(value):
        if isinstance(value,list):return [expand(v) for v in value]
        if not isinstance(value,dict):return value
        if '$ref' in value:return expand(document['$defs'][value['$ref'].split('/')[-1]])
        if isinstance(value.get('type'),list):
            return {'anyOf':[expand({**value,'type':kind}) for kind in value['type']]}
        return {key:expand(v) for key,v in value.items()}
    helper=Path(__file__).with_name('schema_contract.py')
    if not helper.exists():helper=Path(__file__).resolve().parents[2]/'packages/agentstack_mail/src/agentstack_mail/schema_contract.py'
    api=runpy.run_path(str(helper))
    schemas={key:expand(value) for key,value in document['$defs'].items()}
    for schema in schemas.values():api['validate_schema_definition'](schema)
    return schemas,api['validate_schema']


def validate(value, name):
    schemas,validator=contract()
    validator(value,schemas[name],'INSTRUCTION_PLAN_CHANGED',error_type=Refused)


def read_private(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1 or st.st_mode & 0o077:
        raise Refused('INSTRUCTION_PATH_UNSAFE', path)
    return json.loads(path.read_bytes())


@contextmanager
def mutexes(install, paths):
    handles = []
    locked_paths = []
    try:
        folder = install / 'backups/instruction-migration/locks'; private_dir(folder)
        for path in sorted(set(map(str, paths))):
            lock = folder / (sha(path.encode()) + '.lock')
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            st = os.fstat(fd)
            if st.st_uid != os.getuid() or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600:
                os.close(fd); raise Refused('INSTRUCTION_PATH_UNSAFE', lock)
            fcntl.flock(fd, fcntl.LOCK_EX); handles.append(fd); locked_paths.append(lock)
        # Publishers can inventory the exact artifacts this namespace uses;
        # callers which only need serialization continue to ignore the value.
        yield tuple(locked_paths)
    finally:
        for fd in reversed(handles):
            os.close(fd)


def git_state(path):
    r = subprocess.run(['git', '-C', str(path.parent), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if r.returncode:
        return 'untracked' if 'not a git repository' in r.stderr else 'unknown'
    r = subprocess.run(['git', '-C', r.stdout.strip(), 'ls-files', '--error-unmatch', '--', str(path)], capture_output=True)
    return 'tracked' if r.returncode == 0 else ('untracked' if r.returncode == 1 else 'unknown')


class Engine:
    def __init__(self, provider, *, env=None, cwd=None):
        self.env = dict(os.environ if env is None else env)
        self.provider = provider
        self.cwd = Path(cwd or os.getcwd()).absolute()
        self.install = Path(self.env.get('AGENTSTACK_HOME') or str(Path(self.env['HOME']) / '.agentstack')).absolute()
        self.project = self.env.get('AGENTSTACK_PROJECT_KEY') or ''
        self.legacy_project = self.env.get('AGENTSTACK_LEGACY_PROJECT_KEY') or self.project
        if provider == 'claude':
            scope = self.env.get('AGENTSTACK_CLAUDE_MD_SCOPE', '')
            if scope not in ('', 'global'):
                raise Refused('INSTRUCTION_SCOPE_REQUIRES_MIGRATION', scope)
            home = Path(self.env.get('CLAUDE_CONFIG_DIR') or str(Path(self.env['HOME']) / '.claude')).absolute()
            if self.env.get('CLAUDE_HOME') and Path(self.env['CLAUDE_HOME']).resolve() != home.resolve():
                raise Refused('INSTRUCTION_HOME_CONFLICT', 'CLAUDE_HOME is deprecated; use CLAUDE_CONFIG_DIR')
            name = 'CLAUDE.md'
        else:
            home = Path(self.env.get('CODEX_HOME') or str(Path(self.env['HOME']) / '.codex')).absolute(); name = 'AGENTS.md'
        self.home, self.target = home, home / name
        self.template_home = Path(self.env.get('AGENTSTACK_TEMPLATE_HOME') or self.install)
        self.template = (self.template_home / provider / name).read_bytes()
        self.block = render(provider, self.template, str(self.install), self.project)
        self.target_entry, _ = snapshot(self.target)
        self.ledger = self.install / 'backups/instruction-migration' / provider / sha(self.target_entry['resolved_path'].encode())
        self.receipt_path = self.ledger / 'receipt.json'
        runtime = Path(self.env.get('AGENTSTACK_RUNTIME_DIR') or self.install / 'runtime').resolve()
        if self.ledger.resolve().is_relative_to(runtime) or runtime.is_relative_to(self.ledger.resolve()):
            raise Refused('INSTRUCTION_PATH_UNSAFE', 'migration ledger overlaps runtime')
        self.catalog = json.loads((Path(__file__).parent / 'instruction_templates.json').read_bytes())

    def receipt(self):
        if not self.receipt_path.exists() and not self.receipt_path.is_symlink():
            return None
        return read_receipt(self.receipt_path)

    def known(self, data, path, receipt=None):
        proof = self.provenance(data, path, receipt)
        return proof['template_id'] if proof else None

    def provenance(self, data, path, receipt=None):
        block = block_bytes(data)
        if block is None:
            return None
        if receipt and sha(block) == receipt['plan']['target']['after_block_sha256']:
            previous = receipt['plan']['target']
            return {'template_id': previous['template_id'], 'render_inputs': previous['render_inputs']}
        provider = parse(data)[2]
        # Deterministic selection also when a historical template did not use
        # every placeholder: separate-process plans must have identical bytes.
        candidates = sorted({self.project, self.legacy_project, str(path.parent), ''})
        templates = ([{'template_id': sha(self.template), 'template': self.template.decode(),
                       'renderer_id': 'literal-v2'}] if provider == self.provider else [])
        templates += [item for item in self.catalog if item['provider'] == provider]
        for item in templates:
            for project in candidates:
                try:
                    rendered = render(provider, item['template'].encode(), str(self.install), project, item['renderer_id'])
                except Refused:
                    continue
                if block.replace(b'\r\n', b'\n') == rendered:
                    return {'template_id': item['template_id'], 'render_inputs': {
                        'install_home': str(self.install), 'project_key': project or None,
                        'renderer_id': item['renderer_id']}}
        return None

    def source_paths(self, explicit=(), detect=False):
        paths = {Path(x).absolute() for x in explicit}
        if any(x.name not in ('CLAUDE.md', 'AGENTS.md') for x in paths):
            raise Refused('ARGUMENTS_INVALID', '--path must name CLAUDE.md or AGENTS.md')
        if self.provider == 'claude':
            for project in {self.project, self.legacy_project} - {''}:
                paths.add(Path(project).absolute() / 'CLAUDE.md')
        for folder in (self.cwd, *self.cwd.parents):
            if detect or self.provider == 'claude':
                paths.add(folder / 'CLAUDE.md')
            if detect:
                paths.add(folder / 'AGENTS.md')
        distributed = {self.template_home / 'claude/CLAUDE.md', self.template_home / 'codex/AGENTS.md'}
        return sorted(paths - distributed - {self.target}, key=str)

    def cleanup_argv(self, path):
        values={'HOME':self.env['HOME'],'AGENTSTACK_HOME':str(self.install),
            'AGENTSTACK_TEMPLATE_HOME':str(self.template_home),
            'CLAUDE_CONFIG_DIR':str(self.home) if self.provider=='claude' else self.env.get('CLAUDE_CONFIG_DIR',''),
            'CODEX_HOME':str(self.home) if self.provider=='codex' else self.env.get('CODEX_HOME',''),
            'CLAUDE_HOME':'','AGENTSTACK_CLAUDE_MD_SCOPE':'global',
            'AGENTSTACK_PROJECT_KEY':self.project,'AGENTSTACK_LEGACY_PROJECT_KEY':self.legacy_project}
        return ['env',*(key+'='+value for key,value in values.items()),
            str(self.install/f'bin/agentstack-{self.provider}-setup'),'--remove-legacy-block','--path',str(path)]

    def finding(self, path, data, receipt=None):
        try:
            block = block_bytes(data or b'')
            kind = 'missing' if block is None else ('legacy' if self.known(data, path, receipt) else 'edited')
            reason = None if kind != 'edited' else 'template provenance unknown; compare locally and remove only the marker span after review'
        except Refused as exc:
            block, kind, reason = None, 'malformed', str(exc)
        return {'source_path': str(path), 'kind': kind, 'template_id': self.known(data, path, receipt) if kind == 'legacy' else None,
                'block_sha256': sha(block) if block else None, 'reason': reason,
                'cleanup_argv': self.cleanup_argv(path) if kind == 'legacy' and path != self.target else []}

    def detect(self, explicit=()):
        findings = []; receipt = self.receipt()
        for path in [self.target, *self.source_paths(explicit, detect=True)]:
            try:
                entry, data = snapshot(path, source_candidate=path != self.target and path not in {Path(x).absolute() for x in explicit})
                if data is None:
                    if path == self.target:
                        findings.append(self.finding(path, data))
                    continue
                f = self.finding(path, data, receipt)
                if entry['resolved_path'] == self.target_entry['resolved_path'] and f['block_sha256'] == sha(self.block):
                    f['kind'] = 'current';f['cleanup_argv']=[]
                elif path != self.target and f['kind'] == 'legacy':
                    f['kind'] = 'duplicate' if any(x['source_path']==str(self.target) and x['kind']=='current' for x in findings) else 'legacy'
                if f['kind'] != 'missing' or path == self.target:
                    findings.append(f)
            except (OSError, Refused, ValueError) as exc:
                findings.append({'source_path':str(path),'kind':'unknown','template_id':None,'block_sha256':None,'reason':str(exc),'cleanup_argv':[]})
        return findings

    def file_plan(self, path, *, target=False, receipt=None, source_candidate=False):
        entry, data = snapshot(path, source_candidate=source_candidate)
        old = data or b''
        try:
            b = block_bytes(old)
            proof = self.provenance(old, path, receipt)
            known = proof['template_id'] if proof else None
        except Refused:
            if target:raise
            # Malformed sources are retained in this same receipt, so purge
            # cannot silently erase their backups while their marker remains.
            b,known,proof=None,None,None
        if target and b and not known:
            raise Refused('INSTRUCTION_PLAN_CHANGED', 'edited target; compare block locally before applying')
        after = replace_block(old, self.block) if target else (replace_block(old, b'') if known else old)
        entry.update(before_sha256=sha(data) if data is not None else None, after_sha256=sha(after),
                     before_block_sha256=sha(b) if b else None, after_block_sha256=sha(self.block) if target else (None if known else (sha(b) if b else None)),
                     template_id=sha(self.template) if target else known,
                     render_inputs=(proof['render_inputs'] if not target and proof else {'install_home':str(self.install),'project_key':self.project or None,'renderer_id':'literal-v2' if target else 'legacy-sed-v1'}),
                     git_state='not-source' if target else git_state(path), strategy='atomic-referent' if target or known else 'read-only')
        return entry

    def plan(self, explicit=(), *, replan=False, source_only=False):
        r = self.receipt()
        if r and r['stage'] not in TERMINAL and not replan:
            raise Refused('INSTRUCTION_OPERATION_PENDING', f'run --resume --plan-id {r["plan_id"]}')
        target = self.file_plan(self.target, target=True, receipt=r)
        sources, seen = [], {target['resolved_path']}
        for path in (sorted({Path(x).absolute() for x in explicit}, key=str) if source_only else self.source_paths(explicit)):
            try:
                entry = self.file_plan(path, receipt=r, source_candidate=path not in {Path(x).absolute() for x in explicit})
                if entry['resolved_path'] in seen:
                    continue
                if entry['before_block_sha256'] is None:
                    data=snapshot(path, source_candidate=path not in {Path(x).absolute() for x in explicit})[1]
                    if not has_marker(data):
                        continue
                sources.append(entry); seen.add(entry['resolved_path'])
            except Refused as exc:
                if exc.reason == 'INSTRUCTION_HARDLINK_REQUIRES_MANUAL_COPY' or source_only:
                    raise
                continue
            except (OSError, ValueError):
                if source_only:raise
                # Unknown sources are reported by detection, never blindly removed.
                continue
        return {'provider':self.provider,'resolved_home':str(self.home.resolve()),'target':target,'sources':sources,'selection_argv':sum((['--path',str(Path(x).absolute())] for x in sorted(set(explicit))), []),'template_sha256':sha(self.template)}

    def backup(self, entry, plan_id):
        if entry['before_sha256'] is None:
            return None
        path = self.ledger / 'backups' / plan_id / (sha(entry['resolved_path'].encode()) + '.before')
        if path.exists() or path.is_symlink():
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_nlink != 1 or sha(path.read_bytes()) != entry['before_sha256']:
                raise Refused('INSTRUCTION_PLAN_CHANGED', path)
            return str(path)
        data = verify(entry, entry['before_sha256']); private_dir(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        sync_dir(path.parent)
        return str(path)

    def save(self, r, stage=None):
        if stage:
            r['stage'] = stage
        r['updated_at'] = stamp(); validate(r, 'receipt'); atomic(self.receipt_path, canonical(r) + b'\n')

    def confirm(self, assume_yes):
        if assume_yes:
            return 'assume-yes-known-only'
        if not sys.stdin.isatty():
            raise Refused('ARGUMENTS_INVALID', 'confirmation required; run in a terminal or explicitly choose --assume-yes')
        print('Move known ORRERY instructions to the user file and remove old blocks? Tracked/shared files may affect other users. Full-file backups remain private until uninstall --purge-data. [Y/n] ', end='', file=sys.stderr, flush=True)
        return 'interactive-keep' if input().strip().lower() in ('n','no') else 'interactive-remove'

    def apply(self, plan, plan_id, *, assume_yes=False, supersede=None):
        if sha(canonical(plan)) != plan_id:
            raise Refused('INSTRUCTION_PLAN_CHANGED', 'plan ID does not match current inputs')
        old=self.receipt()
        if old and old['stage']=='complete' and plan['target']['before_sha256']==plan['target']['after_sha256'] and not plan['sources']:
            return old
        paths = [e['resolved_path'] for e in [plan['target'],*plan['sources']]]
        with mutexes(self.install, paths):
            old = self.receipt()
            if old and old['stage'] not in TERMINAL and (not supersede or old['plan_id'] != supersede or assume_yes):
                raise Refused('INSTRUCTION_OPERATION_PENDING', 'explicit interactive replan required')
            for entry in [plan['target'],*plan['sources']]:
                verify(entry, entry['before_sha256'])
            # Creating a new file needs no permission to rewrite existing user
            # content. Keep the decision shared by both provider entry points.
            changes_existing = any(e['before_sha256'] is not None and
                                   e['before_sha256'] != e['after_sha256']
                                   for e in [plan['target'], *plan['sources']])
            consent = (self.confirm(assume_yes) if changes_existing or supersede
                       else 'not-required')
            if old:
                atomic(self.ledger / 'history' / (old['plan_id'] + '.json'), canonical(old) + b'\n')
            r = {'kind':'orrery-instruction-migration-v2','plan_id':plan_id,'plan':plan,'consent':consent,'stage':'planned','target_backup':None,'sources_progress':[], 'supersedes': old['plan_id'] if old else None,'updated_at':stamp()}
            self.save(r)
            return self._resume(r)

    def resume(self, plan_id):
        r = self.receipt()
        if not r:
            for old_path in (self.install/'backups/instruction-migration'/self.provider).glob('*/receipt.json'):
                old=read_receipt(old_path)
                if old.get('plan_id')==plan_id:raise Refused('INSTRUCTION_HOME_CHANGED','restore the recorded provider home before resume')
        if not r or r['plan_id'] != plan_id:
            raise Refused('INSTRUCTION_PLAN_CHANGED', 'receipt ID mismatch')
        if r['plan']['resolved_home'] != str(self.home.resolve()) or r['plan']['target']['requested_path'] != str(self.target):
            raise Refused('INSTRUCTION_HOME_CHANGED')
        with mutexes(self.install, [e['resolved_path'] for e in [r['plan']['target'], *r['plan']['sources']]]):
            return self._resume(self.receipt())

    def _resume(self, r):
        p = r['plan']; target = p['target']
        if p['template_sha256'] != sha(self.template) or target['render_inputs']['install_home'] != str(self.install) or target['render_inputs']['project_key'] != (self.project or None):
            raise Refused('INSTRUCTION_HOME_CHANGED', 'render inputs changed')
        if r['stage'] in ('complete','uninstalled'):
            return r
        current = snapshot(target['requested_path'])[1]
        digest = sha(current) if current is not None else None
        if digest == target['before_sha256']:
            verify(target, target['before_sha256'])
            r['target_backup'] = self.backup(target,r['plan_id'])
            self.save(r,'backed_up')
            # Back up every source before any user-file mutation.
            for entry in p['sources']:
                key=sha(entry['resolved_path'].encode())
                if not any(x['path_sha256']==key for x in r['sources_progress']):
                    backup=self.backup(entry,r['plan_id'])
                    r['sources_progress'].append({'path_sha256':key,'backup':backup,'stage':'backed_up'});self.save(r)
            self.save(r,'writing')
            if target['before_sha256'] != target['after_sha256']:
                atomic(target['resolved_path'],replace_block(current or b'',self.block),entry=target)
        elif digest != target['after_sha256']:
            raise Refused('INSTRUCTION_PLAN_CHANGED', target['requested_path'])
        verify(target,target['after_sha256'],completed=True)
        self.save(r,'target_verified')
        retained=False
        for entry in p['sources']:
            self.save(r,'sources_pending')
            progress=next(x for x in r['sources_progress'] if x['path_sha256']==sha(entry['resolved_path'].encode()))
            verify(target,target['after_sha256'],completed=True)
            allowed=entry['strategy']=='atomic-referent' and r['consent'] in ('interactive-remove','assume-yes-known-only') and (r['consent']=='interactive-remove' or entry['git_state']=='untracked')
            if not allowed:
                progress['stage']='retained';retained=True;self.save(r);continue
            data=snapshot(entry['requested_path'])[1]
            digest=sha(data) if data is not None else None
            if digest==entry['before_sha256']:
                atomic(entry['resolved_path'],replace_block(data,b''),entry=entry)
            elif digest!=entry['after_sha256']:
                raise Refused('INSTRUCTION_PLAN_CHANGED',entry['requested_path'])
            verify(entry,entry['after_sha256'],completed=True)
            progress['stage']='complete';self.save(r)
        self.save(r,'retained' if retained else 'complete')
        return r

    def uninstall(self, *, dry_run=False):
        r=self.receipt()
        if not r:
            return None
        if r['stage'] not in TERMINAL:
            raise Refused('INSTRUCTION_OPERATION_PENDING','resume before uninstall')
        target=r['plan']['target']
        with mutexes(self.install,[target['resolved_path']]) if not dry_run else _read_only():
            entry,data=snapshot(target['requested_path']);block=block_bytes(data or b'')
            if block and sha(block)!=target['after_block_sha256']:
                raise Refused('INSTRUCTION_PLAN_CHANGED','edited managed block retained')
            if block and not dry_run:
                entry['before_sha256']=sha(data)
                self.backup(entry,sha(canonical({'uninstall':r['plan_id'],'before':sha(data)})))
                atomic(entry['resolved_path'],replace_block(data,b''),entry=entry)
            if not dry_run:
                self.save(r,'uninstalled')
        return r

    def remove_source(self, path, *, dry_run=False):
        path=Path(path).absolute()
        if path == self.target or path.name not in ('CLAUDE.md','AGENTS.md') or path in (self.template_home/'claude/CLAUDE.md',self.template_home/'codex/AGENTS.md'):
            raise Refused('ARGUMENTS_INVALID','not a user instruction source')
        # Cleanup never installs or updates the user target. The current
        # rendered block must already be present before removing this source.
        _, data = snapshot(self.target)
        if block_bytes(data or b'') != self.block:
            raise Refused('INSTRUCTION_TARGET_REQUIRES_SETUP', 'set up the user block first; cleanup changes only the selected source')
        p=self.plan([str(path)], source_only=True)
        if any(entry['strategy'] != 'atomic-referent' for entry in p['sources']):
            raise Refused('INSTRUCTION_DETECTION_UNKNOWN', 'compare locally and remove only the reviewed marker span manually')
        if dry_run:
            return p
        return self.apply(p,sha(canonical(p)))



@contextmanager
def _read_only():
    yield


def read_receipt(path):
    """All current/history readers share schema and canonical plan integrity."""
    r = read_private(path)
    validate(r, 'receipt')
    if r.get('kind') != 'orrery-instruction-migration-v2' or sha(canonical(r['plan'])) != r['plan_id']:
        raise Refused('INSTRUCTION_PLAN_CHANGED', f'receipt invalid: {path}; keep ledger/backups and restore a verified receipt before retrying')
    return r


def purge_admission(install):
    """Read-only before any uninstaller effect; unknown progress keeps backups."""
    folder=Path(install)/'backups/instruction-migration'
    if not folder.exists():return []
    receipts=[]
    for provider in ('claude','codex'):
        root=folder/provider
        if root.is_symlink():raise Refused('INSTRUCTION_BACKUP_PURGE_BLOCKED',root)
        if not root.exists():continue
        for slot in root.iterdir():
            if slot.is_symlink() or not slot.is_dir():raise Refused('INSTRUCTION_BACKUP_PURGE_BLOCKED',slot)
            r=read_receipt(slot/'receipt.json');receipts.append(str(slot/'receipt.json'))
            if r['stage'] not in TERMINAL:
                raise Refused('INSTRUCTION_BACKUP_PURGE_BLOCKED','pending instruction migration')
            history=slot/'history'
            if history.is_symlink():raise Refused('INSTRUCTION_BACKUP_PURGE_BLOCKED',history)
            generations=[r]
            if history.exists():
                for path in history.iterdir():
                    if path.suffix != '.json':raise Refused('INSTRUCTION_BACKUP_PURGE_BLOCKED',path)
                    previous=read_receipt(path);generations.append(previous)
            for generation in generations:
                for entry in generation['plan']['sources']:
                    _,data=snapshot(entry['requested_path'])
                    if has_marker(data):
                        raise Refused('INSTRUCTION_BACKUP_PURGE_BLOCKED','retained legacy block in current/history receipt; remove after local review')
            _,data=snapshot(r['plan']['target']['requested_path'])
            b=block_bytes(data or b'')
            if b and sha(b)!=r['plan']['target']['after_block_sha256']:
                raise Refused('INSTRUCTION_BACKUP_PURGE_BLOCKED','edited user block')
    return receipts


def uninstall_recorded(install, references, *, dry_run=False, purge=False):
    install=Path(install).absolute()
    if purge:purge_admission(install)
    for reference in references:
        try:
            path=Path(reference)
            if not path.is_relative_to(install/'backups/instruction-migration') or path.name!='receipt.json':
                raise Refused('INSTRUCTION_PATH_UNSAFE','invalid install receipt reference')
            r=read_receipt(path);plan=r['plan'];provider=plan['provider']
            env={'HOME':str(install.parent),'AGENTSTACK_HOME':str(install),'AGENTSTACK_PROJECT_KEY':plan['target']['render_inputs']['project_key'] or '',
                 'CLAUDE_CONFIG_DIR':plan['resolved_home'],'CODEX_HOME':plan['resolved_home']}
            e=Engine(provider,env=env)
            if e.receipt_path!=path:raise Refused('INSTRUCTION_PATH_UNSAFE','receipt target mismatch')
            e.uninstall(dry_run=dry_run)
        except (Refused,OSError,ValueError,KeyError) as exc:
            if purge:raise
            print('warn: kept user instruction block: '+str(exc),file=sys.stderr)
    print('Private full-file instruction backups are retained; uninstall --purge-data removes them after all blocks are resolved.')


def disk_state(findings):
    """One aggregate for doctor/UI; findings remain the detailed truth."""
    kinds={f['kind'] for f in findings}
    if 'unknown' in kinds:return 'unknown'
    if kinds & {'edited','malformed'}:return 'needs_review'
    if 'duplicate' in kinds or {'current','legacy'} <= kinds:return 'legacy_present'
    if 'legacy' in kinds:return 'update_required'
    if 'current' in kinds:return 'clean'
    return 'not_configured'


def instruction_status(provider, home, wrapper, cwd, observation=None):
    # Explicit provider HOME/cwd only. Never inherit the dashboard operator's context.
    if not isinstance(cwd,(str,Path)) or not str(cwd) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise Refused('INSTRUCTION_CWD_UNPROVEN')
    env={'HOME':str(Path(home).parent),'AGENTSTACK_HOME':str(wrapper),
         'AGENTSTACK_TEMPLATE_HOME':str(wrapper),
         'CLAUDE_CONFIG_DIR':str(home),'CODEX_HOME':str(home)}
    engine=Engine(provider,env=env,cwd=cwd)
    findings=engine.detect()
    session='unknown'
    if observation and observation.get('provider')==provider and observation.get('cwd')==str(Path(cwd).absolute()):
        before=observation.get('candidates',[])
        current={(f['source_path'],f['block_sha256']) for f in findings if f['block_sha256']}
        if any((f['source_path'],f['block_sha256']) not in current for f in before if f['block_sha256']):
            session='reload_required'
        elif before:session='candidates_unchanged'
    result={'provider':provider,'target_path':str(engine.target),'findings':findings,
            'checked_at':stamp(),'disk_state':disk_state(findings),'session_state':session,
            'evidence_kind':'candidate-observation-not-effective-load','reason':None}
    validate(result,'status')
    return result


def instruction_observation(provider, home, wrapper, cwd):
    result=instruction_status(provider,home,wrapper,cwd)
    return {'provider':provider,'cwd':str(Path(cwd).absolute()),'observed_at':result['checked_at'],
            'candidates':[{k:f[k] for k in ('source_path','block_sha256','template_id','kind')}
                          for f in result['findings'] if f['block_sha256']]}


def provider_instructions(home, provider, wrapper, project, *, configure=False):
    if provider not in ('claude','codex'):return
    name='CLAUDE.md' if provider=='claude' else 'AGENTS.md'
    template=(Path(wrapper)/provider/name).read_bytes()
    block=render(provider,template,str(wrapper),project)
    path=Path(home)/name
    if path.is_symlink():
        raise Refused('INSTRUCTION_PROVIDER_CONFIG_REQUIRED','generated instruction must remain inside the fixed provider HOME')
    entry,data=snapshot(path)
    if not configure:
        if block_bytes(data or b'')!=block:
            raise Refused('INSTRUCTION_PROVIDER_CONFIG_REQUIRED','run instruction-configure operator '+provider)
        return
    entry['before_sha256']=sha(data) if data is not None else None
    atomic(entry['resolved_path'],replace_block(data or b'',block),entry=entry)


def cli(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider',choices=('claude','codex'),default='claude')
    group=parser.add_mutually_exclusive_group()
    for flag in ('print','check','plan','apply','resume','replan','uninstall','remove-legacy-block'):
        group.add_argument('--'+flag,action='store_true')
    parser.add_argument('--plan-id');parser.add_argument('--expected-plan-id');parser.add_argument('--supersede-plan-id')
    parser.add_argument('--path',action='append',default=[])
    parser.add_argument('--assume-yes',action='store_true');parser.add_argument('--dry-run',action='store_true');parser.add_argument('--json',action='store_true')
    parser.add_argument('--message',choices=tuple(MESSAGES));parser.add_argument('--session-notices',action='store_true')
    args=parser.parse_args(argv)
    if args.message:
        print(MESSAGES[args.message]);return 0
    action=next((x for x in ('print','check','plan','apply','resume','replan','uninstall','remove_legacy_block') if getattr(args,x)), 'apply').replace('_','-')
    try:
        if args.dry_run and not (args.uninstall or args.remove_legacy_block):raise Refused('ARGUMENTS_INVALID','--dry-run is only supported for cleanup/uninstall')
        e=Engine(args.provider)
        if args.print:
            sys.stdout.buffer.write(e.block);return 0
        findings=e.detect(args.path)
        if args.session_notices:
            count=sum(x['kind'] not in ('current','missing') for x in findings)
            if count:print(f'ORRERY instruction migration: {count} notices; ask the operator to run agentstack-doctor.')
            return 0
        result={'schema_version':2,'action':action,'provider':args.provider,'target_path':str(e.target),'stage':None,'plan_id':None,'plan':None,'apply_argv':[],'findings':findings,'reason':None,'next_argv':[]}
        status=0
        if args.check:
            r=e.receipt();result['stage']=r['stage'] if r else None;result['plan_id']=r['plan_id'] if r else None
            status=int(any(x['kind']!='current' for x in findings))
            if r and r['stage'] not in TERMINAL:
                status=1;result['reason']='INSTRUCTION_OPERATION_PENDING';result['next_argv']=[str(e.install/f'bin/agentstack-{args.provider}-setup'),'--resume','--plan-id',r['plan_id']]
        elif args.uninstall:
            r=e.uninstall(dry_run=args.dry_run);result['stage']=r['stage'] if r else 'uninstalled'
        elif args.resume:
            r=e.resume(args.plan_id);result['stage']=r['stage'];result['plan_id']=r['plan_id'];status=int(r['stage']=='retained')
        elif args.remove_legacy_block:
            if len(args.path)!=1:raise Refused('ARGUMENTS_INVALID','one --path required')
            p=e.remove_source(args.path[0],dry_run=args.dry_run)
            result['stage']=p.get('stage');status=int(p.get('stage')=='retained')
        else:
            old=e.receipt()
            if args.replan and (not old or old['plan_id']!=args.expected_plan_id):
                raise Refused('INSTRUCTION_PLAN_CHANGED','expected receipt mismatch')
            p=e.plan(args.path,replan=args.replan or bool(args.supersede_plan_id))
            pid=sha(canonical(p));result['plan']=p;result['plan_id']=pid
            result['apply_argv']=[str(e.install/f'bin/agentstack-{args.provider}-setup'),'--apply','--plan-id',pid,*p['selection_argv']]
            if args.replan:result['apply_argv']+=['--supersede-plan-id',args.expected_plan_id]
            if not args.plan and not args.replan:
                r=e.apply(p,args.plan_id or pid,assume_yes=args.assume_yes,supersede=args.supersede_plan_id)
                result['stage']=r['stage'];status=int(r['stage']=='retained')
        if result['stage'] and not args.dry_run:
            findings=e.detect(args.path);result['findings']=findings
        if args.json:print(json.dumps(result,ensure_ascii=False))
        else:
            if args.check:
                count=sum(x['kind'] not in ('current','missing') for x in findings)
                if count:print(f'warn: {count} instruction migration notices; see individual file paths and cleanup commands below')
            for f in findings:
                label='ok' if f['kind']=='current' else 'warn'
                title='Claude CLAUDE.md' if args.provider=='claude' else 'Codex AGENTS.md'
                update=shlex.join(['env','AGENTSTACK_HOME='+str(e.install),('CLAUDE_CONFIG_DIR=' if args.provider=='claude' else 'CODEX_HOME=')+str(e.home),'AGENTSTACK_PROJECT_KEY='+e.project,str(e.install/f'bin/agentstack-{args.provider}-setup')])
                if f['kind']=='current':description='matches the installed template'
                elif f['kind']=='missing':description='managed block not found in '+f['source_path']+'; add it with: '+update
                elif f['kind']=='malformed':description='is malformed ('+str(f['reason'])+'); compare locally before updating'
                else:description='differs from the installed template; update with: '+update
                print(f'{label}: {title} managed block in {f["source_path"]}; block hash {f["block_sha256"] or "unknown"}'+('; cleanup: '+shlex.join(f['cleanup_argv']) if f['cleanup_argv'] else '')+(('; '+f['reason']) if f['reason'] else '')+'; '+description)
            if result['stage']:print(f'instruction migration: {result["stage"]}; receipt {e.receipt_path}; full-file private backups retained until uninstall --purge-data')
            if result['next_argv']:print('next: '+shlex.join(result['next_argv']))
        return status
    except (Refused,OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        reason=exc.reason if isinstance(exc,Refused) else 'INSTRUCTION_DETECTION_UNKNOWN'
        if args.json:print(json.dumps({'reason':reason,'detail':str(exc)}))
        elif args.check and isinstance(exc,FileNotFoundError):
            title='Claude CLAUDE.md' if args.provider=='claude' else 'Codex AGENTS.md'
            print(f'warn: cannot check {title} managed block: template not found; restore the install with scripts/install.sh; AGENTSTACK_HOME={shlex.quote(os.environ.get("AGENTSTACK_HOME",""))} ; {exc}')
            return 1
        else:print(f'{reason}: {exc}',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(cli())
