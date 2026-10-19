"""Internal fixed legacy adapter for the SAME explicit-configure producer.

This is not a public launcher or native admission. The fixed outer consumer
must supply authenticated identity-only parent observation using the existing
Mail owner-auth branch. No default transport/credential fallback exists here.
The hooks described by this proposal are not yet a usable execution consumer;
ordinary launch remains unsupported until that consumer has been connected.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

from local_model_auth import (LocalModelError, secure_read, private_directory, source_evidence,
                              executable_evidence, canonical_digest)
from runtime_intent_store import owner_lock, read_owner, write_owner, checked_intent
from runtime_legacy_context import checked_legacy_context, configuration_metadata

CHANGED='LOCAL_MODEL_CONFIGURE_PLAN_CHANGED'


def legacy_instruction_output(target, renderer_sources):
    """Pure output bytes from the admitted target; never user migration.

    The fixed configure/registration consumers admit the install root before
    calling this renderer. Like other resource producers, this does not grant
    target authority or use the active producer's checkout as a fallback.
    """
    from instruction_blocks import render
    target=checked_legacy_context(target)
    root=Path(target['install_root'])
    template=root/'claude/CLAUDE.md'
    try:
        proof=renderer_sources[str(template)]
        if proof.get('state')!='present':raise ValueError()
        expected={str(template):proof}
        if source_evidence([str(template)])!=expected:raise ValueError()
        raw=secure_read(template,os.getuid())
        if hashlib.sha256(raw).hexdigest()!=proof['sha256']:raise ValueError()
        block=render('claude',raw,str(root),target['project_key'])
        if source_evidence([str(template)])!=expected:raise ValueError()
        return {'path':str(Path(target['owner']).with_suffix('.preview-home')/'CLAUDE.md'),
            'kind':'provider-instructions','mode':0o600,'content':block.decode('utf-8')}
    except (OSError,ValueError,TypeError,KeyError):raise LocalModelError(CHANGED) from None


def legacy_proxy_sources(root):
    """Closed dependencies of the existing stdlib proxy, not the App daemon."""
    package=Path(root)/'integrations/codex_app/src/agentstack_codex_app'
    return [str(package/name) for name in (
        'agent_mail_client.py','hook_entry.py','identity_store.py','snapshot.py','mcp_server.py')]


def legacy_proxy_output(target,scope,python,renderer_sources):
    """Mail-only proposal; the actual entry remains unsupported until connected."""
    target=checked_legacy_context(target)
    try:
        root=Path(target['install_root'])
        paths=legacy_proxy_sources(root)
        expected={path:renderer_sources[path] for path in paths}
        if (any(row.get('state')!='present' for row in expected.values())
                or source_evidence(paths)!=expected
                or canonical_digest(executable_evidence(python['path']))!=canonical_digest(python)):
            raise ValueError()
        prefixes={tool.rsplit('__',1)[0] for tool in scope['mail_tools']}
        if len(prefixes)!=1:raise ValueError()
        prefix=prefixes.pop()
        if not prefix.startswith('mcp__') or not re.fullmatch('[A-Za-z0-9_-]+',prefix[5:]):raise ValueError()
        definition={'command':python['path'],'args':['-I','-B',
            str(root/'bin/lib/runtime_legacy_preview_cli.py'),'entry','proxy',target['name']]}
        config={'mcpServers':{name:definition for name in sorted({'orrery-mail',prefix[5:]})}}
        return {'path':str(Path(target['owner']).with_suffix('.preview-home')/'local-model-mcp.json'),
            'kind':'provider-settings','mode':0o600,
            'content':json.dumps(config,sort_keys=True,ensure_ascii=False)}
    except (OSError,ValueError,TypeError,KeyError,AttributeError):raise LocalModelError(CHANGED) from None


def check_legacy_provider_outputs(intent):
    """Read-only generated resources; no native permission/skill approval."""
    from runtime_provider_projection import _build_provider_projection
    evidence=intent['configuration_evidence']
    target=intent['legacy_context']
    home=Path(target['owner']).with_suffix('.preview-home')
    projection=_build_provider_projection(intent,{'intent_owner':target['owner']})
    outputs=(legacy_instruction_output(intent['legacy_context'],evidence['renderer_sources']),
        legacy_proxy_output(intent['legacy_context'],intent['effective_scope'],
            evidence['runtime_python'],evidence['renderer_sources']),
        {'path':str(home/'local-model-settings.json'),
         'content':json.dumps(projection['provider_config']['settings'],sort_keys=True,ensure_ascii=False)})
    for output in outputs:
        raw=secure_read(output['path'],os.getuid(),mode=0o600,parent_mode=0o700)
        if raw!=output['content'].encode():raise LocalModelError('LOCAL_MODEL_CONFIGURATION_CHANGED')
    _check_legacy_skill_links(home,evidence['skill_sources'])


def _check_legacy_skill_links(home,sources):
    """Exact selected entry set, no-follow dirfds, link identity and target."""
    from runtime_model_resources import skill_outputs
    changed='LOCAL_MODEL_CONFIGURATION_CHANGED'
    directory=None
    def identity(info):
        # Readlink/listdir may update atime. Pin identity and mutation fields,
        # not the timestamp of our own read.
        return (info.st_dev,info.st_ino,info.st_uid,info.st_gid,info.st_mode,
            info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    try:
        expected=skill_outputs(list(sources),home/'skills')['outputs']
        expected={Path(row['path']).name:row['target'] for row in expected}
        parent=private_directory(home,os.getuid(),exact_mode=0o700)
        try:
            try:entry=os.stat('skills',dir_fd=parent,follow_symlinks=False)
            except FileNotFoundError:
                if expected:raise ValueError()
                return
            if not stat.S_ISDIR(entry.st_mode):raise ValueError()
            directory=private_directory(home/'skills',os.getuid(),exact_mode=0o700)
            before=os.fstat(directory)
            if (entry.st_dev,entry.st_ino)!=(before.st_dev,before.st_ino):raise ValueError()
            if set(os.listdir(directory))!=set(expected):raise ValueError()
            for name,target in expected.items():
                info=os.stat(name,dir_fd=directory,follow_symlinks=False)
                if (not stat.S_ISLNK(info.st_mode) or info.st_uid!=os.getuid()
                        or info.st_nlink!=1 or os.readlink(name,dir_fd=directory)!=target
                        or identity(os.stat(name,dir_fd=directory,follow_symlinks=False))!=identity(info)):raise ValueError()
            if identity(os.fstat(directory))!=identity(before):raise ValueError()
            if identity(os.stat('skills',dir_fd=parent,follow_symlinks=False))!=identity(entry):raise ValueError()
        finally:os.close(parent)
    except (OSError,ValueError,TypeError,KeyError):raise LocalModelError(changed) from None
    finally:
        if directory is not None:os.close(directory)


def legacy_hook_settings(python, install_root, name):
    """Deterministic settings proposal, not evidence these hooks have run."""
    # The actual entry implementation remains fail-closed until exec binding
    # exists. Never substitute global hooks or legacy re-registration here.
    script=str(Path(install_root)/'bin/lib/runtime_legacy_preview_cli.py')
    def hook(operation):
        return {'type':'command','command':shlex.join([str(python),script,'entry',operation,name])}
    edit='Edit|Write|MultiEdit|NotebookEdit'
    return {'hooks':{'SessionStart':[{'hooks':[hook('session-start')]}],
        'PreToolUse':[{'matcher':edit,'hooks':[hook('pre-edit')]}],
        'PostToolUse':[{'matcher':edit,'hooks':[hook('post-edit')]},{'hooks':[hook('notifications')]}],
        'SessionEnd':[{'hooks':[hook('end-session')]}]}}


class LegacyConfigurationClient:
    """Private candidate owner/context adapter; never a fictional global client.

    _authenticate_parent is an internal fixed-consumer interface, not an HTTP
    field, callback selected by a CLI argument, or caller-supplied assertion.
    The public operator entry uses the fixed shared Mail owner-auth consumer;
    tests substitute only its peer, not the owner authentication branch.
    """
    mode='legacy'
    identity=None

    def __init__(self,target,*,_authenticate_parent):
        self.target=checked_legacy_context(target)
        root=Path(__file__).resolve().parents[2]
        if self.target['install_root']!=str(root) or not callable(_authenticate_parent):
            raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
        self.config={'wrapper_root':str(root)}
        self.runtime_dir=Path(self.target['runtime_dir'])
        self.client_root=self.runtime_dir
        self.profile_path=Path(self.target['owner'])
        # The fixed install/runtime preparation owns these shared containers.
        # Requiring them avoids creating a planned output ancestor as a side
        # effect of taking the name lock, without relaxing output ownership.
        try:
            for directory in (self.runtime_dir,self.profile_path.parent):
                fd=private_directory(directory,os.getuid(),exact_mode=0o700);os.close(fd)
        except (OSError,ValueError):raise LocalModelError(CHANGED) from None
        self.path=root/'env.sh'
        self.reader=root/'hooks/project-context.sh'
        self._authenticate_parent=_authenticate_parent

    def context_bytes(self):
        # Local reads only. This function is also used inside the owner fence;
        # neither HTTP nor the installed settings-reader subprocess runs there.
        try:
            env=secure_read(self.path,os.getuid())
            reader=secure_read(self.reader,os.getuid())
            mail=secure_read(Path(self.config['wrapper_root'])/
                'integrations/codex_app/src/agentstack_codex_app/agent_mail_client.py',os.getuid())
            value={'target':checked_legacy_context(self.target),
                'env_sha256':hashlib.sha256(env).hexdigest(),
                'reader_sha256':hashlib.sha256(reader).hexdigest(),
                'mail_client_sha256':hashlib.sha256(mail).hexdigest()}
            return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
        except (OSError,ValueError,TypeError):raise LocalModelError(CHANGED) from None

    def authenticate_target(self):
        """Read-only authentication OUTSIDE locks; no key/provider execution."""
        try:
            self.validate_output(self.profile_path,'profile')
            before=self.context_bytes()
            expected={'AGENTSTACK_RUNTIME_MODE':'legacy',
                'AGENTSTACK_PROJECT_KEY':self.target['project_key'],
                'AGENTSTACK_MCP_URL':self.target['mail_url']}
            for key,value in expected.items():
                # Reuse the existing installer's literal reader. Do not source
                # env.sh or invent a second shell/value parser in Python.
                result=subprocess.run(['/bin/bash',str(self.reader),'installed-env-value',key,str(self.path)],
                    env={'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin','LC_ALL':'C'},
                    stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5,check=False)
                if result.returncode or result.stdout!=value.encode():raise LocalModelError(CHANGED)
            identity=self._authenticate_parent()
            if (type(identity) is not dict or type(identity.get('id')) is not int
                    or type(identity.get('project_id')) is not int
                    or identity!={**self.target['parent'],'project_id':self.target['project_id']}):
                raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
            if self.context_bytes()!=before:raise LocalModelError(CHANGED)
            return before
        except LocalModelError:raise
        except Exception:raise LocalModelError('LOCAL_MODEL_AUTH_FAILED') from None

    def fixed_context(self,value):
        context=deepcopy(value)
        if (context.get('operator_uid')!=os.getuid() or context.get('mode')!='legacy'
                or context.get('legacy_context')!=self.target):raise LocalModelError(CHANGED)
        # Unlike global's implicit profile, legacy uses the explicit candidate
        # child-state path. Never silently rewrite a contradictory target.
        if (context.get('client_root')!=str(self.client_root)
                or context.get('intent_owner')!=str(self.profile_path)
                or context.get('runtime_client')!=str(Path(self.config['wrapper_root'])/'bin/lib/runtime_client.py')):
            raise LocalModelError(CHANGED)
        return context

    def provider_home(self,provider):
        if provider!='claude':raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
        return self.profile_path.with_suffix('.preview-home')

    def framework_settings(self, python):
        return legacy_hook_settings(python,self.config['wrapper_root'],self.target['name'])

    def _intent_metadata(self,intent):
        try:
            intent=checked_intent(intent)
            if intent.get('legacy_context')!=self.target:raise LocalModelError(CHANGED)
            caller={key:intent[key] for key in ('name','parent','provider','detached','worktree','chrome','tools')}
            if intent['model']['kind']!='local-profile':raise LocalModelError(CHANGED)
            caller['model']=intent['model']['ref']
            if 'task_delivery' in intent:caller['task_delivery']=intent['task_delivery']
            return configuration_metadata(caller,{'mode':'legacy','operator_uid':os.getuid(),
                'client_root':str(self.client_root),'intent_owner':str(self.profile_path),
                'runtime_client':str(Path(self.config['wrapper_root'])/'bin/lib/runtime_client.py'),
                'legacy_context':self.target,'execution_policy':intent['execution_policy']})
        except (KeyError,TypeError,ValueError):raise LocalModelError(CHANGED) from None

    def profile_frame(self,intent,previous):
        self._intent_metadata(intent)
        self._check_candidate(previous)
        return {**previous,'configuration_stage':'prepared','launch_intent':intent}

    def _check_candidate(self,previous):
        credential=self.runtime_dir/('agent_token_'+self.target['name'])
        if credential.exists() or credential.is_symlink():
            raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        if not previous:return
        # First release is configure-before-registration only. It cannot adopt
        # an old cloud owner/credential or rewrite a completed execution record.
        if (set(previous)!={'configuration_stage','launch_intent'}
                or previous['configuration_stage']!='prepared'
                or checked_intent(previous['launch_intent']).get('legacy_context')!=self.target):
            raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        self._intent_metadata(previous['launch_intent'])

    def validate_output(self,path,kind):
        if Path(path)!=self.profile_path or kind!='profile':raise LocalModelError(CHANGED)
        previous=(read_owner(self.profile_path)
            if self.profile_path.exists() or self.profile_path.is_symlink() else {})
        self._check_candidate(previous)

    def write_output(self,path,value,kind):
        self.validate_output(path,kind)
        write_owner(path,value)

    def operation_mutex(self):return owner_lock(self.profile_path,'legacy')

    @contextmanager
    def fence(self):
        before=self.context_bytes()
        yield
        if self.context_bytes()!=before:raise LocalModelError(CHANGED)


def instruction_configure(provider,action,payload,*,approved_id=None):
    """Existing operator plan/apply surface, explicitly selected legacy only.

    This is not dashboard input and does not register/start a child. The only
    parent credential selected is the current operator's canonical AGENT_NAME;
    the typed target/caller must match before that credential can be read.
    """
    from runtime_caller_schema import canonical_caller
    from runtime_local_configure import plan_local_configuration,apply_local_configuration
    from runtime_legacy_mail import configuration_parent_authenticator
    from local_model_auth import canonical_digest
    try:
        if provider!='claude':raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
        if action=='local-plan':
            if type(payload) is not dict or set(payload)!={'caller','context','resources'}:
                raise LocalModelError('CLIENT_INPUT_INVALID')
            fields={'local_models_root','effective_scope','approved_settings_sha256',
                'approved_settings_digests','auth_context','execution_policy','legacy_context'}
            context=payload['context'];resources=payload['resources']
            required={'python','cli','declared_cli_version','settings_paths','skill_sources','native_sources'}
            if (type(context) is not dict or set(context)-fields
                    or type(resources) is not dict or not required<=set(resources)
                    or set(resources)-required-{'auth_dependencies'}):raise LocalModelError('CLIENT_INPUT_INVALID')
            caller=canonical_caller(payload['caller'])
            target=checked_legacy_context(context['legacy_context'])
            context={**context,'mode':'legacy','operator_uid':os.getuid(),'client_root':target['runtime_dir'],
                'intent_owner':target['owner'],'runtime_client':str(Path(target['install_root'])/'bin/lib/runtime_client.py')}
        elif action=='local-apply':
            if (type(payload) is not dict or payload.get('kind')!='orrery-local-configure-plan-v1'
                    or not approved_id or payload.get('plan_id')!=approved_id
                    or canonical_digest({key:value for key,value in payload.items() if key!='plan_id'})!=approved_id):
                raise LocalModelError(CHANGED)
            caller=canonical_caller(payload['inputs']['caller']);context=deepcopy(payload['inputs']['context'])
            target=checked_legacy_context(context['legacy_context'])
        else:raise LocalModelError('ARGUMENTS_UNSUPPORTED')
        if (caller['provider']!=provider or caller['parent']!=target['parent']['name']
                or os.environ.get('AGENT_NAME')!=target['parent']['name']):
            raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        configuration_metadata(caller,context)
        client=LegacyConfigurationClient(target,_authenticate_parent=configuration_parent_authenticator(target))
        if action=='local-plan':return plan_local_configuration(client,caller,context,**resources)
        return apply_local_configuration(client,payload,approved_id=approved_id)
    except LocalModelError:raise
    except Exception:raise LocalModelError(CHANGED) from None
