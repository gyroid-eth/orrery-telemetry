"""Receipt-only pilot plugin. A full file-reservation guard is a later stage."""
import json
import os
from pathlib import Path

from mail_lifecycle import read, save, stamp
from mail_probe import mail_receipt


MAIL_TOOL = 'mcp__orrery_mail__send_message'


def install(ctx, state):
    state = Path(state)
    config, identity, attempt = [read(state / name) for name in
                               ('run-config.json', 'mail-identity.json', 'attempt.json')]
    expected = read(state / 'message.json')
    profile = Path(config['profile_home']).resolve()
    if Path(os.environ.get('HERMES_HOME', '')).resolve() != profile:
        raise ValueError('Mail pilot profile mismatch')
    if attempt['agent_name'] != identity['agent_name'] or identity['parent'] != config['parent']:
        raise ValueError('Mail pilot identity mismatch')

    def before(tool_name, args, **_):
        if tool_name == MAIL_TOOL:
            if args != expected:
                return {'action': 'block', 'message': 'Only the fixed parent Mail probe is authorized'}
            try:
                save(state / 'report-intent.json', {'attempt_id': attempt['attempt_id'],
                     'agent_name': identity['agent_name'], 'at': stamp()}, exclusive=True)
            except OSError:
                return {'action': 'block', 'message': 'A send was already attempted; parent must resolve its receipt'}
        # Keep memory/skills available; do not let a cloud profile read a local
        # profile via an explicit session-search selector.
        if args.get('profile') not in (None, '', config['profile_name']):
            return {'action': 'block', 'message': 'Only the current profile is authorized'}
        if tool_name in ('skill_view', 'skill_manage'):
            for key in ('name', 'path', 'category'):
                value = args.get(key)
                if isinstance(value, str) and (Path(value).is_absolute() or '..' in Path(value).parts):
                    return {'action': 'block', 'message': 'Skill paths must stay in the current profile'}
        return None

    def after(tool_name, result, **_):
        if tool_name != MAIL_TOOL:
            return
        receipt = mail_receipt(result)
        if receipt is None:
            return  # Unknown is pending, not a successful model claim.
        valid = (receipt['ok'] and receipt['from'] == identity['agent_name']
                 and receipt['to'] in (config['parent'], [config['parent']]))
        save(state / 'mail-send-receipt.json', {**receipt, 'ok': bool(valid),
             'attempt_id': attempt['attempt_id'], 'agent_name': identity['agent_name'],
             'source': 'product-bound-proxy', 'checked_at': stamp()}, exclusive=True)

    ctx.register_hook('pre_tool_call', before)
    ctx.register_hook('post_tool_call', after)
    save(state / 'mail-plugin-ready.json', {'attempt_id': attempt['attempt_id'],
         'agent_name': identity['agent_name'], 'profile_home': str(profile), 'at': stamp()})


def register(ctx):
    state = os.environ.get('ORRERY_HERMES_STATE_ROOT')
    if not state or not Path(state).is_absolute():
        raise ValueError('An explicit Mail pilot state is required')
    install(ctx, state)
