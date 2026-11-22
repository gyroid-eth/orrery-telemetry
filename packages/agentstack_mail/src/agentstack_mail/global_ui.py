"""Operator-only reads; one transaction and the public S2b visibility/query core."""
import copy
import math
import os

from .global_server import GlobalError
from .global_s2a import revision
from .global_s2b_contract import TOOLS, validate, ensure_budget
from .global_ui_contract import REQUEST, RESPONSE



class OperatorRead:
    def _management_apply(self, request, peer_uid):
        if not isinstance(request, dict) or request.get('action') != 'ui_read':
            return super()._management_apply(request, peer_uid)
        if peer_uid != os.getuid():
            raise GlobalError('PEER_UID_REQUIRED')
        validate(request, REQUEST)
        binding = tuple(request[k] for k in ('expected_server_instance_id', 'candidate_generation', 'authority_epoch'))
        q = request['query']
        # Shared transaction fences/tracker and BEGIN DEFERRED protect the entire
        # projection, MAX, keyset, owner selection and response revision.
        with self.transaction(binding=binding) as db:
            data = self.ui_query(db, q)
            result = dict(ok=True, binding=dict(zip(('expected_server_instance_id', 'candidate_generation', 'authority_epoch'), binding)),
                          as_of_revision=revision(db), query_kind=q['kind'], data=data)
            if q['kind'] == 'messages':
                result['tool'] = q['tool']
            ensure_budget(result)
            validate(result, RESPONSE, 'RESPONSE_INVALID')
            return result

    def ui_query(self, db, q):
        kind = q['kind']
        if kind == 'roster':
            rows = list(db.execute('SELECT id AS agent_id,name,program,model,task_description,last_active_ts,retired_at FROM agents WHERE (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?',
                                   (q['before_agent_id'], q['before_agent_id'], q['limit']+1)))
            return dict(items=[dict(r) for r in rows[:q['limit']]], next_before_agent_id=rows[q['limit']-1]['agent_id'] if len(rows)>q['limit'] else None)
        if kind == 'resolve_name':
            from .utils import sanitize_agent_name
            if not q['name'] or sanitize_agent_name(q['name']) != q['name']:
                raise GlobalError('NAME_INVALID')
            row = db.execute('SELECT id FROM agents WHERE lookup_key=?', (q['name'].lower(),)).fetchone()
            return {'agent_id': row[0] if row else None}
        if kind == 'health':
            signal = self.signal_stats(db)
            age = signal['oldest_undelivered_age_seconds']
            return dict(server_ready=True, signal=dict(status=signal['status'],pending_count=signal['undelivered_count'],oldest_pending_age_seconds=None if age is None else max(0, math.floor(age))))
        owner = db.execute('SELECT * FROM agents WHERE id=?', (q['view_as_agent_id'],)).fetchone()
        if owner is None:
            raise GlobalError('TARGET_UNAVAILABLE')
        if kind == 'message':
            row = self.visible(db, q['message_id'], owner['id'])
            if row is None:
                raise GlobalError('MESSAGE_UNAVAILABLE')
            return self.projection(db, row, owner['id'], True)
        if kind == 'messages':
            tool = q['tool']
            supplied = q['arguments']
        else:
            tool = 'search_messages'
            supplied = dict(query='', limit=q['limit'], cursor=q['cursor'], include_bodies=False)
        schema = TOOLS[tool]['input_schema']
        args = {k:copy.deepcopy(v['default']) for k,v in schema['properties'].items() if 'default' in v}
        args.update(supplied, agent_id=owner['id'])
        if kind == 'activity':
            for source,target in (('target_agent_id','_ui_target'),('after_id','_ui_after')):
                if source in q:
                    args[target] = q[source]
        if kind == 'edge_messages':
            args['_ui_edge'] = [q['a_agent_id'], q['b_agent_id']]
            args['_ui_kind'] = kind
        result = self.query(db, tool, owner, args)
        if kind in ('activity', 'edge_messages'):
            result.pop('attachment', None)
        return result
