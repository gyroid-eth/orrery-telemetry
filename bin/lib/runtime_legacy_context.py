"""Literal legacy target binding in the sole intent, never server authentication.

The fixed operator/consumer must ALSO authenticate the parent against this
target using the existing owner-auth branch. A well-formed dictionary or URL
alone cannot prove which server answered, approve execution, or release a key.
No new mode selector, target registry, resolver, CLI or transport is added here.
"""
from copy import deepcopy
from pathlib import Path
import re
from urllib.parse import urlsplit

from local_model_auth import LocalModelError, plain_text
from runtime_preview_policy import validate_preview_policy

INVALID='LOCAL_MODEL_LEGACY_CONTEXT_INVALID'
FIELDS={'schema_version','install_root','runtime_dir','owner','name','project_key',
        'project_id','mail_url','parent'}
NAME=r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}'


def _path(value):
    if (type(value) is not str or not plain_text(value) or not Path(value).is_absolute()
            or '..' in Path(value).parts or str(Path(value))!=value
            or str(Path(value).resolve())!=value):raise ValueError()
    return Path(value)


def checked_legacy_context(value):
    """Closed, nonsecret configuration data. Does not observe a Mail server."""
    try:
        if (type(value) is not dict or set(value)!=FIELDS
                or type(value['schema_version']) is not int or value['schema_version']!=1
                or type(value['name']) is not str or not re.fullmatch(NAME,value['name'])
                or type(value['project_id']) is not int or value['project_id']<=0):raise ValueError()
        for key in ('install_root','runtime_dir','owner','project_key'):_path(value[key])
        if Path(value['owner'])!=Path(value['runtime_dir'])/'child-agents'/(value['name']+'.json'):
            raise ValueError()
        parent=value['parent']
        if (type(parent) is not dict or set(parent)!={'id','name','program','model'}
                or type(parent['id']) is not int or parent['id']<=0
                or type(parent['name']) is not str or not re.fullmatch(NAME,parent['name'])
                or parent['name']==value['name']
                or any(not plain_text(parent[key]) for key in ('program','model'))):raise ValueError()
        if not plain_text(value['mail_url']):raise ValueError()
        url=urlsplit(value['mail_url'])
        if (url.scheme not in ('http','https') or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment
                or '..' in url.path.split('/') or not url.path.startswith('/')
                or (url.scheme=='http' and url.hostname not in ('127.0.0.1','::1','localhost'))):
            raise ValueError()
        # Accessing port also rejects malformed/out-of-range port syntax.
        if url.port is not None and not 1<=url.port<=65535:raise ValueError()
        return deepcopy(value)
    except (KeyError,TypeError,ValueError,OSError,RuntimeError):
        raise LocalModelError(INVALID) from None


def configuration_metadata(caller, context):
    """Bind only explicitly configured preview inputs; ordinary paths unchanged.

    The caller grammar still does not accept these fields. Only the operator
    context may carry them. This initial legacy consumer is detached, Claude,
    cold and embedded-task only; unsupported shapes never fall back to cloud.
    """
    if not {'execution_policy','legacy_context'} & context.keys():return {}
    try:
        if context.get('mode')!='legacy':raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
        target=checked_legacy_context(context['legacy_context'])
        policy=validate_preview_policy(context['execution_policy'],provider=caller['provider'],
            model_kind='local-profile' if caller['model'].startswith('local:') else 'catalog')
        if (caller['name']!=target['name'] or caller['parent']!=target['parent']['name']
                or context['intent_owner']!=target['owner'] or context['client_root']!=target['runtime_dir']
                or context['runtime_client']!=str(Path(target['install_root'])/'bin/lib/runtime_client.py')):
            raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        if (caller['detached'] is not True or caller['worktree'] is not False or caller['chrome'] is not False
                or caller.get('task_delivery','embedded')!='embedded' or caller['tools']!={}):
            raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
        return {'execution_policy':policy,'legacy_context':target}
    except LocalModelError:raise
    except (KeyError,TypeError,ValueError,AttributeError):
        raise LocalModelError(INVALID) from None
