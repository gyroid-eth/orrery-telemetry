"""Actual caller front doors under a disposable delivery-only binding.

This adapter never synthesizes a capture, owner intent, R2 receipt or PID.
Unimplemented product inputs remain real rejections, not successful traces.
Native permission/auth/tool acceptance is deliberately outside this adapter.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

from .caller_binding import (make_delivery_python, read_binding_trace,
                             assert_single_replacement, assert_real_delivery_gates)
from .caller_payload import ROOT, copy_installer_payload
from .evidence import CALLERS
from .caller_server import FreshServerLifetime, wait_fixture_health


class DeliveryCallerProbe:
    def __init__(self, workspace, *, enabled=True):
        self.workspace = workspace
        self.servers = FreshServerLifetime(workspace)
        self.install = workspace.tmp / 'install'
        self.payload = copy_installer_payload(self.install)
        self.python, self.trace = make_delivery_python(workspace, self.install, enabled=enabled)
        self.enabled = enabled
        # Shell entrypoints invoke python3. This external-command fixture only
        # fixes its interpreter spelling; it does not replace their parsers.
        python3 = workspace.tmp / 'bin/python3'
        import shlex
        python3.write_text('#!/bin/sh\nexec '+shlex.quote(str(self.python))+' "$@"\n')
        python3.chmod(0o500)
        self.settings = workspace.tmp / 'configure-native.toml'
        self.settings.write_text('')
        self.settings.chmod(0o600)

    def environment(self):
        ws = self.workspace
        return {'HOME':str(ws.tmp / 'home'), 'TMPDIR':str(ws.tmp / 'tmp'),
            'PATH':str(ws.tmp / 'bin')+':/usr/bin:/bin', 'LANG':'C',
            'AGENTSTACK_RUNTIME_MODE':'global', 'AGENTSTACK_GLOBAL_SERVER_CONFIG':str(self.config),
            'AGENTSTACK_MCP_URL':self.url, 'AGENTSTACK_CONTACT_POLICY':'skip',
            'AGENTSTACK_CODEX_BIN':str(ws.tmp / 'bin/fixture-provider')}

    def run_entry(self, action, data):
        path = self.workspace.tmp / ('entry-'+uuid.uuid4().hex+'.json')
        path.write_text(json.dumps(data, sort_keys=True))
        path.chmod(0o600)
        env = self.environment()
        if data['entry'] == 'dashboard':
            env['AGENTSTACK_HOME'] = str(self.workspace.tmp/'isolation/agentstack')
            env['AGENTSTACK_SPAWN_ROOTS'] = str(self.workspace.tmp)
        return subprocess.run([str(self.python), str(Path(__file__).with_name('caller_entry.py')),
            action, str(path)], env=env, capture_output=True, text=True, timeout=45)

    def __call__(self, entry, *, workspace, **options):
        assert workspace is self.workspace
        if entry == 'pending-replay':
            # These are still red until real interrupt/reconstruction wiring
            # exists. A synthetic pending dict or trace would weaken #266.
            raise NotImplementedError('R3_ACTUAL_PENDING_REPLAY_BINDING_UNCONNECTED')
        # Prior resume state is an oracle input only, never a wrapper that may
        # authenticate against the new authority. Read it before discarding the
        # stopped server's owned temporary subtree.
        assert Path(options['owner']) == workspace.tmp/'isolation/agentstack/clients/child_01/profile.json'
        approved_intent = None
        if entry == 'resume-child' and Path(options['owner']).exists():
            approved_intent = json.loads(Path(options['owner']).read_text())['launch_intent']
        with self.servers.entry() as live:
            self.config, self.url = live.config, live.url
            return self.invoke_current(entry, workspace=workspace,
                                       approved_intent=approved_intent, **options)

    def invoke_current(self, entry, *, workspace, approved_intent, **options):
        assert options['real_entry'] == CALLERS[entry]
        data = {'entry':entry, 'caller':deepcopy(workspace.caller),
                'context':deepcopy(workspace.context), 'settings':str(self.settings),
                'server_config':str(self.config), 'provider_binary':str(options['provider_binary']),
                'task_delivery':options.get('task_delivery', 'mail'),
                'provider_session_id':options.get('provider_session_id')}
        assert options.get('headless', False) is False, 'interactive capture contract only'
        assert options.get('fresh_same_context', True) is True
        execute_operator = options.get('operator_execute',False)
        assert type(execute_operator) is bool
        assert not execute_operator or (entry=='operator' and workspace.caller['detached'] is False)
        expected_prepare = ((entry=='operator' and not execute_operator)
                            or (entry in ('shell','dashboard') and bool(options.get('inspect_prepare_only'))))
        assert options.get('prepare_only', expected_prepare) is expected_prepare
        if execute_operator:data['operator_prepare_only'] = False
        if options.get('operator_bad_pin'):
            assert entry=='operator' and expected_prepare and not execute_operator
            data['operator_bad_pin'] = True
        assert Path(options['owner']) == workspace.tmp/'isolation/agentstack/clients/child_01/profile.json'
        if options.get('fixture_mcp'):
            mcp = options['fixture_mcp']
            assert mcp == {'name':'fixture_extra', 'definition':{'command':'/usr/bin/true', 'args':[]}}
            data['fixture_mcp'] = mcp
        # A new explicit configure producer accepts only known source layers.
        # Keep the old unknown sentinel untouched; it is not silently dropped.
        scope = data['context']['effective_scope']
        scope['settings_sources'] = [str(self.settings)]
        data['context']['approved_settings_sha256'] = hashlib.sha256(self.settings.read_bytes()).hexdigest()
        prepared = self.run_entry('setup', data)
        assert prepared.returncode == 0, 'R3_CALLER_SETUP_REJECTED: '+prepared.stderr
        data.update(json.loads(prepared.stdout))
        if 'operator_caller_overrides' in options:
            # Deliberately different incoming API values, AFTER the owner was
            # configured. Backfilling from that owner must not rescue them.
            assert entry == 'operator' and options.get('inspect_prepare_only')
            overrides = options['operator_caller_overrides']
            assert isinstance(overrides,dict) and len(overrides)==1
            data['operator_caller_overrides'] = deepcopy(overrides)
        if options.get('inspect_prepare_only'):
            assert entry in ('operator','shell','dashboard') and options['prepare_only'] is True
            data['inspect_prepare_only'] = True
        if 'dashboard_caller_overrides' in options:
            assert entry=='dashboard' and options.get('inspect_prepare_only')
            overrides = options['dashboard_caller_overrides']
            assert isinstance(overrides,dict) and len(overrides)==1
            data['dashboard_caller_overrides'] = deepcopy(overrides)
        if entry == 'dashboard':
            from .caller_dashboard import prepare_dashboard
            prepare_dashboard(self, data)
            if options.get('dashboard_bad_pin'):
                assert not options.get('inspect_prepare_only')
                data['dashboard_bad_pin'] = True
        if entry == 'resume-child':
            from .caller_resume import prepare_real_resume
            prepare_real_resume(self, data, approved_intent=approved_intent, options=options)
        if entry == 'shell':
            from .caller_shell import prepare_shell
            prepare_shell(self, data)
            if options.get('shell_bad_pin'):
                assert not options.get('inspect_prepare_only')
                data['shell_bad_pin'] = True
        if options.get('real_entry_mutation'):
            assert options['real_entry_mutation'] == 'no-op'
            assert entry in ('prepare-child','resume-child','operator','shell','dashboard')
            if entry != 'shell':
                data['real_entry_mutation'] = {'kind':'no-op',
                    'trace':str(workspace.tmp/('mutation-'+entry+'.jsonl'))}
        if 'invalid_api_tools' in options:
            data['invalid_api_tools'] = options['invalid_api_tools']
        callback = options.get('on_before_real_entry')
        if callback:
            callback()
        trace_before = len(read_binding_trace(self.trace))
        if entry == 'shell':
            from .caller_shell import shell_argv, shell_mutation
            argv = shell_argv(self,data,options)
            env = self.environment()
            env['AGENTSTACK_CLIENT_CONFIG'] = data['parent_context']
            with shell_mutation(self,options):
                result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=45)
        else:
            result = self.run_entry('invoke', data)
        rows = read_binding_trace(self.trace)[trace_before:]
        self.last_entry_rows = rows
        assert_single_replacement(rows, enabled=self.enabled)
        for relative, source in self.payload['sources'].items():
            assert (self.install/relative).read_bytes() == source.read_bytes(), 'fixture modified product payload: '+relative
        # Never produce substitute captures or swallow unrelated parser errors.
        if entry == 'dashboard' and result.returncode == 0:
            self.last_dashboard_http = json.loads(result.stdout)
        if options.get('expect_tools_rejection'):
            tools_calls = [row for row in rows if row['event']=='real-call'
                           and row['file']==str(self.install/'hooks/child_tools.py')
                           and row['name'] in ('normalize','parse_cli')]
            assert tools_calls, 'R3_REAL_TOOLS_PARSER_UNREACHED'
            if entry == 'shell':
                return result
            if entry == 'dashboard' and result.returncode == 0:
                return self.last_dashboard_http['body']
        if result.returncode:
            raise RuntimeError('R3_REAL_CALLER_REJECTED: '+entry+': '+result.stderr)
        if entry == 'dashboard' and self.last_dashboard_http['status'] != 200:
            raise RuntimeError('R3_REAL_CALLER_REJECTED: dashboard: '+self.last_dashboard_http['body']['reason'])
        if entry in ('shell','dashboard'):
            if entry == 'shell':
                from .caller_shell import check_shell_task
                check_shell_task(self,data,result,rows)
            else:
                from .caller_dashboard import check_dashboard_task
                check_dashboard_task(self,data,self.last_dashboard_http,rows)
            calls = {(row['file'],row['name']) for row in rows if row['event']=='real-call'}
            assert not any(name in ('start','execute','exec_provider')
                and file==str(self.install/'bin/lib/runtime_delivery.py') for file,name in calls), \
                'prepare-only '+entry+' executed before the delivery helper'
            if options.get('inspect_prepare_only'):
                return result
            # The task already exists and was witnessed in the real entry. This
            # helper must not rerun task preparation and rescue a root no-op.
            result = self.run_entry('execute-'+entry, data)
            rows = read_binding_trace(self.trace)[trace_before:]
            self.last_entry_rows = rows
            assert_single_replacement(rows, enabled=self.enabled)
            for relative, source in self.payload['sources'].items():
                assert (self.install/relative).read_bytes() == source.read_bytes(), 'fixture modified product payload: '+relative
            if result.returncode:
                raise RuntimeError('R3_REAL_CALLER_REJECTED: '+entry+': '+result.stderr)
        if options.get('inspect_prepare_only'):
            calls = {(row['file'],row['name']) for row in rows if row['event']=='real-call'}
            for file, name in [('runtime_operator.py','spawn'),
                               ('runtime_client.py','prepare_child'),
                               ('runtime_client.py','_prepare_child_launch')]:
                assert (str(self.install/'bin/lib'/file),name) in calls, 'real operator preparation was not called'
            assert not any(name in ('start','execute','exec_provider')
                and file==str(self.install/'bin/lib/runtime_delivery.py') for file,name in calls)
            return result
        assert_real_delivery_gates(rows, self.install)
        return result
