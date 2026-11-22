"""Read existing client truth without constructing clients, creating files or SQLite handles."""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
import time
import threading
from datetime import datetime, timezone

_CACHE = {}
_CACHE_LOCK = threading.Lock()


def unproven_app(snapshot,row=None):
    return (row or {}).get('program')=='codex-app' or any('app_state' in x for x in snapshot['lives'])


class Observations:
    def __init__(self, operator, parser, *, deadline=None):
        self.operator = operator
        self.api = operator.api
        self.parser = parser
        self.deadline = time.monotonic() + 5 if deadline is None else deadline

    def budget(self):
        return self.api['remaining'](self.deadline)

    def snapshot(self, root, agent_id, *, lock_held=False):
        """A single noncreating shared-lock entry, also used for revalidation."""
        self.budget()
        api = self.api
        api['safe_private_directory'](root)
        runtime = root / 'runtime'
        api['safe_private_directory'](runtime)
        lock = runtime / 'child.lock'
        if not lock.exists() and not lock.is_symlink():
            raise api['ClientError']('OBSERVATION_MUTEX_UNAVAILABLE')
        with (nullcontext() if lock_held else api['observation_inode'](lock)):
            values = {}
            def read(name, path, optional=False):
                self.budget()
                if optional and not path.exists() and not path.is_symlink():
                    values[name] = None
                    return None
                value = api['read_json'](path)
                values[name] = value
                return value
            context = read('context', root / api['CLIENT_LAYOUT']['context'])
            identity = context.get('identity')
            if (context.get('kind') != api['KIND'] or context.get('mode') != 'global'
                    or context.get('activation_enabled') is not False
                    or not isinstance(identity, dict) or identity.get('agent_id') != agent_id
                    or any(context.get(k) != v for k, v in self.operator.binding.items())
                    or context.get('client_name') != root.name
                    or context.get('runtime_root') != str(self.operator.runtime)
                    or context.get('agentstack_home') != str(self.operator.clients_parent.parent)):
                raise api['ClientError']('IDENTITY_BINDING_MISMATCH')
            credential = read('credential', root / api['CLIENT_LAYOUT']['credential'])
            if (credential.get('kind') != 'orrery-global-credential-v1'
                    or credential.get('agent_id') != agent_id
                    or credential.get('credential_generation') != identity.get('credential_generation')
                    or any(credential.get(k) != v for k, v in self.operator.binding.items())):
                raise api['ClientError']('CREDENTIAL_BINDING_MISMATCH')
            launch=read('launch', root / api['CLIENT_LAYOUT']['launch'], True)
            if launch is not None:
                api['delivery_contract']()['validate'](launch,'launch_intent',api['ClientError'])
            child = read('child', root / api['CLIENT_LAYOUT']['child'], True)
            if child is not None:
                api['wire_contract']()['validate_schema'](child, api['child_schema']())
            read('session', runtime / 'session_index/self.json', True)
            pending=read('mutation', root / api['CLIENT_LAYOUT']['mutation'], True)
            values['mutation_sha256']=(hashlib.sha256(api['read_private'](root / api['CLIENT_LAYOUT']['mutation'])).hexdigest() if pending is not None else None)
            lives = runtime / 'live-sessions'
            values['lives'] = []
            if lives.exists() or lives.is_symlink():
                api['safe_private_directory'](lives)
                for path in sorted(lives.iterdir()):
                    self.budget()
                    value = api['read_json'](path)
                    if (value.get('kind') != 'orrery-global-live-session-v1'
                            or value.get('agent_id') != agent_id
                            or not isinstance(value.get('session_id'), str)
                            or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value['session_id']) is None
                            or path.name != value['session_id'] + '.json'
                            or any(value.get(k) != v for k, v in self.operator.binding.items())):
                        raise api['ClientError']('SESSION_OWNER_CONFLICT')
                    values['lives'].append(value)
            self.budget()
            # Hash only the already-read JSON. Never open database bytes.
            digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
            return values, digest

    def owner(self, snapshot):
        self.budget()
        receipt = self.api['management_exchange'](self.operator.config,
            {'version':1,'action':'inspect',**self.operator.binding,
             'agent_id':snapshot['context']['identity']['agent_id']},deadline=self.deadline)
        owner = self.api['validate_owner_receipt'](receipt,snapshot['context']['identity'],
            snapshot['credential'].get('registration_token'),self.operator.binding)
        if receipt.get('retired_at'):
            raise self.api['ClientError']('AGENT_UNAVAILABLE')
        return owner

    def item(self, agent_id, reason=None):
        return dict(agent_id=agent_id, liveness='unknown', act_state=None, ctx_used=None,
                    session_count=0, parent_agent_id=None, parent_state='unknown',
                    activity_freshness='unknown', reason=reason)

    def roster(self):
        rows = {}
        before = None
        while True:
            self.budget()
            page = self.operator.read({'kind':'roster','before_agent_id':before,'limit':200}, deadline=self.deadline)['data']
            rows.update((x['agent_id'], x) for x in page['items'])
            before = page['next_before_agent_id']
            if before is None:
                return rows

    def roots(self):
        result = {}
        parent = self.operator.clients_parent
        if not parent.exists() and not parent.is_symlink():
            return result
        self.api['safe_private_directory'](parent)
        for root in parent.iterdir():
            self.budget()
            try:
                self.api['validate_client_name'](root.name)
                self.api['safe_private_directory'](root)
                context = self.api['read_json'](root / self.api['CLIENT_LAYOUT']['context'])
                identity = context.get('identity') or {}
                aid = identity.get('agent_id')
                if self.api['integer'](aid, 1):
                    result.setdefault(aid, []).append(root)
            except (RuntimeError, OSError, ValueError, TypeError, KeyError):
                continue  # No trustworthy ID; never assign it to a different root.
        return result

    def activity(self, snapshot, digest, row):
        lives = snapshot['lives']
        launch = snapshot['launch']
        live_count, uncertain = 0, False
        parsed = None
        for live in lives:
            self.budget()
            if live.get('ended'):
                continue
            # Standalone slots have no launch receipt; PID/start/pane proof is
            # the same as launched slots. Never synthesize launch state.
            key = (tuple(sorted(self.operator.binding.items())), row['agent_id'], digest, live['session_id'])
            with _CACHE_LOCK:
                hit = _CACHE.get(key)
            if hit and time.monotonic() - hit[0] < 4.5:
                state, sample = hit[1:]
            else:
                state, _, text = self.api['pane_evidence'](live, deadline=self.deadline, lines=45, snapshot=snapshot)
                sample = self.parser(text) if state == 'live' else None
                with _CACHE_LOCK:
                    now = time.monotonic()
                    for old in list(_CACHE):
                        if now - _CACHE[old][0] >= 4.5:
                            del _CACHE[old]
                    _CACHE[key] = (now, state, sample)
            # A leftover launch slot may already be absent. Prove that before
            # treating its stale launch binding as an uncertain live session.
            if state != 'absent' and live.get('launch_id') is not None:
                if (not launch or launch.get('phase') != 'running'
                        or any(live.get(k) != launch.get(k) for k in ('launch_id','receipt_nonce'))):
                    uncertain = True
                    continue
            if state == 'live':
                live_count += 1
                parsed = sample
            elif state != 'absent':
                uncertain = True
        if uncertain:
            return dict(liveness='unknown', session_count=live_count, reason='OBSERVATION_INCOMPLETE')
        if live_count > 1:
            return dict(liveness='live', session_count=live_count, reason='MULTIPLE_LIVE_SESSIONS')
        if not live_count:
            # Empty/missing receipts do not prove the provider has stopped.
            return dict(liveness='absent' if lives else 'unknown', reason=None if lives else 'SESSION_UNAVAILABLE')
        ctx = parsed.get('ctx_used')
        if type(ctx) is not int or not 0 <= ctx <= 100:
            ctx = None
        act = parsed.get('act_state')
        return dict(liveness='live', session_count=1, act_state=act if act in ('work','wait','ask','question') else None,
                    ctx_used=ctx, activity_freshness='current', reason=None)

    def read(self, ids):
        try:
            roster, roots = self.roster(), self.roots()
        except Exception:
            return self.response([self.item(aid, 'OBSERVATION_INCOMPLETE') for aid in ids])
        snapshots = {}
        items = []
        for aid in ids:
            item = self.item(aid)
            try:
                self.budget()
                row = roster.get(aid)
                if row is None or row.get('retired_at'):
                    raise self.api['ClientError']('AGENT_UNAVAILABLE')
                slots = roots.get(aid, [])
                if len(slots) != 1:
                    raise self.api['ClientError']('AMBIGUOUS_CLIENT_ROOT' if slots else 'CLIENT_ROOT_UNAVAILABLE')
                root = slots[0]
                snapshot, digest = self.snapshot(root, aid)
                self.owner(snapshot)
                identity = snapshot['context']['identity']
                generation = row.get('credential_generation')
                if generation is not None and generation != identity.get('credential_generation'):
                    raise self.api['ClientError']('CREDENTIAL_BINDING_MISMATCH')
                if unproven_app(snapshot,row):
                    item['reason'] = 'APP_ACTIVITY_UNPROVEN'
                else:
                    item.update(self.activity(snapshot, digest, row))
                if self.snapshot(root, aid)[1] != digest:
                    raise self.api['ClientError']('LOCAL_OBSERVATION_CHANGED')
                self.owner(snapshot)
                snapshots[aid] = (root, snapshot, digest)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, self.api['ClientError']) else 'OBSERVATION_INCOMPLETE'
                item = self.item(aid, reason)
            items.append(item)
        # Parent reads never hold two client mutexes simultaneously.
        for item in items:
            aid = item['agent_id']
            if aid not in snapshots:
                continue
            try:
                root, snapshot, digest = snapshots[aid]
                child = snapshot['child']
                if child is None:
                    item['parent_state'] = 'none'
                    continue
                parent = child['parent']
                pid = parent['agent_id']
                proots = roots.get(pid, [])
                if (pid == aid or pid not in roster or roster[pid].get('retired_at')
                        or parent['server_instance_id'] != self.operator.binding['expected_server_instance_id']
                        or len(proots) != 1 or proots[0].name != parent['client_name']):
                    continue
                ps, parent_digest = self.snapshot(proots[0], pid)
                ancestry = [(proots[0], pid, ps, parent_digest)]
                self.owner(ps)
                # Follow the existing typed ancestry to reject cycles rather
                # than inventing lineage from names or Mail traffic.
                seen = {aid}
                while ps.get('child') is not None:
                    seen.add(pid)
                    pp = ps['child']['parent']; pid = pp['agent_id']
                    if pid in seen:
                        raise self.api['ClientError']('PARENT_UNAVAILABLE')
                    # Only the direct parent is a condition of this edge.
                    # Stop cycle traversal at an unavailable/retired ancestor;
                    # that ancestor's own edge is independently unknown.
                    if (len(roots.get(pid, [])) != 1
                            or pid not in roster or roster[pid].get('retired_at')):
                        break
                    if roots[pid][0].name != pp['client_name'] or pp['server_instance_id'] != self.operator.binding['expected_server_instance_id']:
                        break
                    ps, parent_digest = self.snapshot(roots[pid][0], pid)
                    ancestry.append((roots[pid][0],pid,ps,parent_digest))
                    self.owner(ps)
                if self.snapshot(root, aid)[1] != digest:
                    raise self.api['ClientError']('LOCAL_OBSERVATION_CHANGED')
                for parent_root, parent_id, parent_snapshot, parent_digest in ancestry:
                    if self.snapshot(parent_root,parent_id)[1] != parent_digest:
                        raise self.api['ClientError']('LOCAL_OBSERVATION_CHANGED')
                    self.owner(parent_snapshot)
                item.update(parent_agent_id=parent['agent_id'], parent_state='known')
            except Exception:
                item.update(parent_agent_id=None, parent_state='unknown')
        return self.response(items)

    def response(self, items):
        return dict(ok=True, runtime_mode='global', binding=self.operator.binding,
                    observed_at=datetime.now(timezone.utc).isoformat(), items=items)
