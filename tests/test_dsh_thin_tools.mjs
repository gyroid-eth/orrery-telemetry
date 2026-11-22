import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {apply, payloadFor, saveSession, callHook} from '../integrations/dsh/orrery_tools.mjs';

function fixture(run) {
  const handlers = new Map();
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-thin-tools-'));
  const config = {hooks_dir: root, cwd: root, state_dir: root, env: {AGENT_NAME: 'OwnChild'}};
  const ctx = {on: (name, fn) => handlers.set(name, fn), workingDirectory: {ensure: async () => path.join(root, 'changed-cwd')}};
  apply(ctx, config, run);
  return {root, config, handlers, clean: () => fs.rmSync(root, {recursive: true, force: true})};
}
const exec = {name: 'write', arguments: {file_path: 'work.txt', content: 'private content'}, agent: {session: {id: 'own-id'}}};

test('native relative edit resolves effective cwd and invokes common registration/reservation only', async () => {
  const calls = [];
  const f = fixture(async (file, payload, env) => {calls.push({file, payload, env}); return {ok: true};});
  try {
    assert.equal(await f.handlers.get('tools/pre-execute')(exec, () => 'NATIVE_ALLOWED'), 'NATIVE_ALLOWED');
    assert.deepEqual(calls.map(c => path.basename(c.file)), ['deny-llm-key-read.py', 'check-agent-registered.sh', 'check-file-reservation.sh']);
    assert.equal(calls[2].payload.tool_name, 'Write');
    assert.equal(calls[2].payload.cwd, path.join(f.root, 'changed-cwd'));
    assert.equal(calls[2].payload.session_id, 'own-id');
    assert.equal(calls[2].env.AGENT_NAME, 'OwnChild');
    assert.equal(calls[2].payload.tool_input.file_path, 'work.txt');
  } finally { f.clean(); }
});

test('common denial prevents the native body; key/read do not call reservation policy', async () => {
  const calls = [];
  const f = fixture(async file => {calls.push(path.basename(file)); return {ok: false, reason: 'common refusal'};});
  try {
    let ran = false;
    assert.deepEqual(await f.handlers.get('tools/pre-execute')({...exec, name: 'bash'}, () => {ran = true;}), {kind: 'deny', reason: 'common refusal'});
    assert.equal(ran, false);
    assert.deepEqual(calls, ['deny-llm-key-read.py']);
  } finally { f.clean(); }
});

test('successful edits use existing release hook; failures and non-edits do not release', async () => {
  const calls = [];
  const f = fixture(async (file, payload) => {calls.push({file, payload}); return {ok: true};});
  try {
    await f.handlers.get('tools/result')(exec, {isError: true});
    await f.handlers.get('tools/result')({...exec, name: 'read'}, {isError: false});
    assert.equal(calls.length, 0);
    await f.handlers.get('tools/result')(exec, {isError: false});
    assert.equal(path.basename(calls[0].file), 'release-file-reservation.sh');
    assert.deepEqual(calls[0].payload.tool_result, {success: true});
    assert.equal(JSON.stringify(calls[0]).includes('private result'), false);
  } finally { f.clean(); }
});

test('native tool set has no independent allowlist and unrelated tools keep native decisions', async () => {
  const f = fixture(async () => ({ok: true}));
  try {
    for (const name of ['search', 'subagent', 'web', 'mcp__orrery_mail__fetch_inbox'])
      assert.equal(await f.handlers.get('tools/pre-execute')({...exec, name}, () => name), name);
    assert.equal(payloadFor({...exec, name: 'edit'}, f.config).tool_name, 'Edit');
  } finally { f.clean(); }
});

test('two child session bindings are separate; save failure does not stop launch', () => {
  const a = fixture(async () => ({ok: true}));
  const b = fixture(async () => ({ok: true}));
  try {
    a.handlers.get('agent/created')({agent: {session: {id: 'A'}}});
    b.handlers.get('agent/created')({agent: {session: {id: 'B'}}});
    for (const [f, id] of [[a, 'A'], [b, 'B']]) {
      const row = JSON.parse(fs.readFileSync(path.join(f.root, 'native-session.json')));
      assert.equal(row.harness, 'dsh');
      assert.equal(row.native_session_id, id);
      assert.ok(row.created_at);
    }
    a.handlers.get('agent/created')({agent: {session: {id: 'SUBAGENT', header: {parentSession: 'A'}}}});
    assert.equal(JSON.parse(fs.readFileSync(path.join(a.root, 'native-session.json'))).native_session_id, 'A');
    assert.doesNotThrow(() => saveSession({session: {id: 'C'}}, {state_dir: '/not-present/never'}));
  } finally { a.clean(); b.clean(); }
});

test('real subprocess hook exit is observed without exposing stderr', async () => {
  const f = fixture(async () => ({ok: true}));
  try {
    const file = path.join(f.root, 'fake-hook.sh');
    fs.writeFileSync(file, '#!/bin/sh\ncat >/dev/null\necho PRIVATE_KEY_LIKE_TEXT >&2\nexit 1\n', {mode: 0o700});
    const result = await callHook(file, {tool_name: 'Read'});
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'ORRERY hook denied: fake-hook.sh');
  } finally { f.clean(); }
});

test('only a successful public systemMessage is visible; other hook output stays private', async t => {
  const f = fixture();
  let visible = '';
  t.mock.method(process.stderr, 'write', text => {visible += text; return true;});
  try {
    const file = path.join(f.root, 'public-hook.sh');
    for (const [stdout, status, expected] of [
      ['', 0, ''],
      ['{"systemMessage":"Public reservation warning"}', 0, 'Public reservation warning\n'],
      ['PRIVATE_STDOUT', 0, ''],
      ['{"private":"PRIVATE_JSON"}', 0, ''],
      ['{"systemMessage":""}', 0, ''],
      ['{"systemMessage":123}', 0, ''],
      ['[{"systemMessage":"PRIVATE_ARRAY"}]', 0, ''],
      ['null', 0, ''],
      ['{"systemMessage":"PRIVATE_FAILED_WARNING"}', 2, ''],
    ]) {
      visible = '';
      fs.writeFileSync(file, '#!/bin/sh\ncat >/dev/null\nprintf "%s" "$FIXTURE_STDOUT"\n' +
        '[ "$FIXTURE_STATUS" = 0 ] && echo PRIVATE_STDERR >&2\nexit "$FIXTURE_STATUS"\n', {mode: 0o700});
      const result = await callHook(file, {tool_name: 'Read', cwd: f.root},
        {...process.env, FIXTURE_STDOUT: stdout, FIXTURE_STATUS: String(status)});
      assert.equal(result.ok, status === 0);
      assert.equal(visible, expected);
      assert.equal(JSON.stringify(result).includes('PRIVATE_'), false);
    }
  } finally {f.clean();}
});

test('only exit 2 carries the bounded public denial into the native tool result', async () => {
  const f = fixture();
  try {
    const file = path.join(f.root, 'deny.sh');
    fs.writeFileSync(file, '#!/bin/sh\ncat >/dev/null\nprintf "%s" "$FIXTURE_STDOUT"\nprintf "%s" "$FIXTURE_STDERR" >&2\nexit "$FIXTURE_STATUS"\n', {mode: 0o700});
    for (const [status, stdout, stderr, expected] of [
      [2, '', 'FILE RESERVATION REQUIRED: work.txt\nAcquire a reservation before editing.', 'FILE RESERVATION REQUIRED: work.txt\nAcquire a reservation before editing.'],
      [2, '{"reason":"Public JSON reason"}', 'PRIVATE_STDERR', 'Public JSON reason'],
      [2, '{"hookSpecificOutput":{"permissionDecisionReason":"Public hook reason"}}', '', 'Public hook reason'],
      [2, 'PRIVATE_NON_JSON', '', ''],
      [2, '{"private":"PRIVATE_JSON"}', '', ''],
      [2, '', 'Traceback (most recent call last):\nPRIVATE_INTERNAL', ''],
      [2, '', '  File "private.py", line 9\nPRIVATE_INTERNAL', ''],
      [2, '', 'TypeError: PRIVATE_INTERNAL', ''],
      [2, '', 'hook.sh: line 9: PRIVATE_INTERNAL', ''],
      ...['Traceback (most recent call last):', 'RuntimeError: PRIVATE_INTERNAL'].flatMap(diagnostic => {
        const colored = '\x1b[31m' + diagnostic + '\x1b[0m';
        return [[2, '', colored, ''], [2, JSON.stringify({reason: colored}), '', '']];
      }),
      [1, '{"reason":"PRIVATE_NON_BLOCKING"}', 'PRIVATE_STDERR', ''],
      [2, '', '拒😀'.repeat(1000), '拒😀'.repeat(1000)],
      [2, '', '拒😀'.repeat(1000) + 'x', '拒😀'.repeat(1000)],
      [2, '', '\x1b[31m公開拒否\x1b[0m\x07', '公開拒否'],
    ]) {
      const result = await callHook(file, {cwd: f.root}, {...process.env, FIXTURE_STATUS: String(status),
        FIXTURE_STDOUT: stdout, FIXTURE_STDERR: stderr});
      assert.equal(result.ok, false);
      assert.equal(result.reason, 'ORRERY hook denied: deny.sh' + (expected ? '\n' + expected : ''));
      assert.equal(result.reason.includes('PRIVATE_'), false);
    }
    fs.writeFileSync(file, '#!/bin/sh\ncat >/dev/null\nprintf "\\377A" >&2\nexit 2\n', {mode: 0o700});
    assert.equal((await callHook(file, {cwd: f.root})).reason, 'ORRERY hook denied: deny.sh\n\ufffdA');
    fs.writeFileSync(file, '#!/bin/sh\ncat >/dev/null\nprintf "%s" "$FIXTURE_STDERR" >&2\nexit 2\n', {mode: 0o700});
    // The adapter returns the same denial to the native tool middleware.
    const g = fixture((_, payload, env) => callHook(file, {...payload, cwd: f.root}, {...env,
      FIXTURE_STATUS: '2', FIXTURE_STDOUT: '', FIXTURE_STDERR: 'Reserve before editing'}));
    try {
      let ran = false;
      const decision = await g.handlers.get('tools/pre-execute')(exec, () => {ran = true;});
      assert.equal(ran, false); assert.equal(decision.kind, 'deny');
      assert.match(decision.reason, /Reserve before editing/);
    } finally {g.clean();}
  } finally {f.clean();}
});

test('hook subprocess follows native cwd for relative reservation checks and release', async () => {
  const f = fixture();
  const originalCwd = process.cwd();
  try {
    const root = fs.realpathSync(f.root);
    const a = path.join(root, 'A'), b = path.join(root, 'B');
    fs.mkdirSync(a); fs.mkdirSync(b);
    for (const dir of [a, b]) fs.writeFileSync(path.join(dir, 'work.txt'), 'unchanged');
    const common = path.resolve('hooks/reservation-common.sh');
    const grant = path.join(root, 'grant'), seen = path.join(root, 'seen');
    const env = {HOME: root, PATH: '/usr/bin:/bin', AGENTSTACK_HOME: path.join(root, 'install'),
      AGENTSTACK_RUNTIME_DIR: path.join(root, 'runtime'), AGENTSTACK_HOOKS_DIR: root,
      AGENTSTACK_PROJECT_KEY: root, AGENTSTACK_PROTECTED_ROOTS: root, AGENTSTACK_PYTHON: '/usr/bin/python3',
      FIXTURE_COMMON: common, FIXTURE_GRANT: grant, FIXTURE_SEEN: seen};
    for (const file of ['deny-llm-key-read.py', 'check-agent-registered.sh'])
      fs.writeFileSync(path.join(root, file), '#!/bin/sh\ncat >/dev/null\nexit 0\n', {mode: 0o700});
    // Exercise the actual product path/project resolver, replacing only the
    // reservation lookup/release transport with a fixture grant and log.
    const resolver = '#!/bin/bash\n. "$FIXTURE_COMMON"\n' +
      'input=$(cat)\nreservation_resolve_tool_context "$input" || exit 2\n' +
      'printf "%s|%s|%s\\n" "$FILE_PATH" "$MATCHED_ROOT" "$PWD" >> "$FIXTURE_SEEN"\n';
    fs.writeFileSync(path.join(root, 'check-file-reservation.sh'), resolver +
      '[ "$FILE_PATH" = "$(cat "$FIXTURE_GRANT")" ] || exit 2\n', {mode: 0o700});
    fs.writeFileSync(path.join(root, 'release-file-reservation.sh'), resolver, {mode: 0o700});
    const handlers = new Map();
    let nativeCwd = a;
    apply({on: (name, fn) => handlers.set(name, fn), workingDirectory: {ensure: async () => nativeCwd}},
      {hooks_dir: root, cwd: a, state_dir: root, env});
    process.chdir(a); // Native working_directory changes its service, not this process cwd.
    nativeCwd = b;
    const edit = {...exec, arguments: {file_path: 'work.txt', content: 'native write'}};
    const write = () => {fs.writeFileSync(path.join(nativeCwd, edit.arguments.file_path), edit.arguments.content); return 'written';};
    fs.writeFileSync(grant, path.join(a, 'work.txt'));
    assert.equal((await handlers.get('tools/pre-execute')(edit, write)).kind, 'deny');
    assert.equal(fs.readFileSync(path.join(b, 'work.txt'), 'utf8'), 'unchanged');
    fs.writeFileSync(grant, path.join(b, 'work.txt'));
    assert.equal(await handlers.get('tools/pre-execute')(edit, write), 'written');
    assert.equal(fs.readFileSync(path.join(a, 'work.txt'), 'utf8'), 'unchanged');
    assert.equal(fs.readFileSync(path.join(b, 'work.txt'), 'utf8'), 'native write');
    await handlers.get('tools/result')(edit, {isError: false});
    assert.deepEqual(fs.readFileSync(seen, 'utf8').trim().split('\n'),
      Array(3).fill([path.join(b, 'work.txt'), root, b].join('|')));
  } finally {process.chdir(originalCwd); f.clean();}
});

test('bash workdir becomes the real hook process cwd after native directory changes', async () => {
  const f = fixture();
  try {
    const root = fs.realpathSync(f.root), changed = path.join(root, 'changed'), nested = path.join(changed, 'nested');
    fs.mkdirSync(nested, {recursive: true});
    const seen = path.join(root, 'bash-cwd');
    for (const file of ['deny-llm-key-read.py', 'check-agent-registered.sh'])
      fs.writeFileSync(path.join(root, file), '#!/bin/sh\ncat >/dev/null\npwd -P >> "$FIXTURE_SEEN"\n', {mode: 0o700});
    const handlers = new Map();
    apply({on: (name, fn) => handlers.set(name, fn), workingDirectory: {ensure: async () => changed}},
      {hooks_dir: root, cwd: root, state_dir: root, env: {FIXTURE_SEEN: seen}});
    assert.equal(await handlers.get('tools/pre-execute')({...exec, name: 'bash', arguments: {command: 'true', workdir: 'nested'}},
      () => 'native body'), 'native body');
    assert.deepEqual(fs.readFileSync(seen, 'utf8').trim().split('\n'), [nested, nested]);
  } finally {f.clean();}
});

const nativeRuntime = process.env.ORRERY_DSH_TEST_RUNTIME;
const nativeNode = process.env.ORRERY_DSH_TEST_NODE;
const skipNative = !nativeRuntime || !nativeNode ? 'explicit pinned native runtime and Node required' : false;

test('native CLI tool dispatch invokes common hooks; broken adapter leaves native tools running', {skip: skipNative}, async () => {
  const {spawnSync} = await import('node:child_process');
  const {pathToFileURL} = await import('node:url');
  for (const mode of ['loaded', 'broken']) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-native-tools-'));
    const save = (name, text, mode = 0o600) => {const file = path.join(root, name);fs.mkdirSync(path.dirname(file), {recursive: true});fs.writeFileSync(file, text, {mode});return file;};
    try {
      const home = path.join(root, 'dsh');
      const profile = path.join(home, 'profiles/probe');
      save('dsh/profiles/probe/package.json', JSON.stringify({name: 'native-tool-probe', private: true, dsh: {profile: {bundles: ['@deepseek-ai/dsh-base']}}}));
      save('dsh/profiles/probe/cordis.patch.yml', '[]');
      fs.symlinkSync(path.join(nativeRuntime, 'node_modules'), path.join(profile, 'node_modules'));
      const hooks = path.join(root, 'hooks');
      const realGuard = process.env.ORRERY_DSH_TEST_KEY_GUARD ?? path.resolve('hooks/deny-llm-key-read.py');
      fs.mkdirSync(hooks);
      fs.copyFileSync(realGuard, path.join(hooks, 'deny-llm-key-read.py'));
      fs.chmodSync(path.join(hooks, 'deny-llm-key-read.py'), 0o700);
      save('hooks/check-agent-registered.sh', '#!/bin/sh\ncat >/dev/null\nprintf \'%s\\n\' \'{"systemMessage":"ORRERY_PUBLIC_HOOK_WARNING"}\'\nexit 0\n', 0o700);
      save('hooks/release-file-reservation.sh', '#!/bin/sh\ncat >/dev/null\nexit 0\n', 0o700);
      save('hooks/check-file-reservation.sh', '#!/bin/sh\ninput=$(cat)\ncase "$input" in *allowed.txt*) exit 0;; *) echo "FILE RESERVATION REQUIRED: reserve before editing" >&2; exit 2;; esac\n', 0o700);
      const secretHelper = save('security', '#!/bin/sh\necho CALLED >>"' + path.join(root, 'helper-calls') + '"\necho SYNTHETIC\n', 0o700);
      const definitions = save('backends.json', JSON.stringify({backends: {strata: {keychain: {service: 'fake-service', account: 'fake-account'}}}}));
      const protectedFile = save('provider-config.json', '{"not_a_real_key":true}');
      const plugin = mode === 'loaded' ? path.resolve('integrations/dsh/orrery_tools.mjs') : save('broken.mjs', 'throw new Error("BROKEN_ADAPTER")');
      const probe = save('probe.mjs', `import fs from 'node:fs';export const inject=['tools','agents'];export function apply(ctx,config){ctx.get('appReady').onReady(async()=>{
const rows=[];
try {
const handle=await ctx.agents.create({sessionId:'native-own-session',meta:{cwd:config.root}});
const child=await ctx.agents.create({sessionId:'native-delegated-session',meta:{cwd:config.root,parentSession:'native-own-session',origin:'subagent'},parentAgent:handle.agent});
const call=(name,args)=>handle.agent.ctx.tools.execute({callId:'probe-'+rows.length,name,agent:handle.agent,arguments:args,signal:new AbortController().signal});
 for(const file of ['unreserved.txt','expired.txt','conflict.txt','allowed.txt']) { const r=await call('write',{file_path:config.root+'/'+file,content:'native write'});rows.push({file,error:r.isError,denial_visible:JSON.stringify(r).includes('FILE RESERVATION REQUIRED: reserve before editing')}); }
 if(config.mode==='loaded') {const read=await call('read',{file_path:config.protected});rows.push({file:'protected-read',error:read.isError});
 const shell=await call('bash',{command:config.helper+' find-generic-password -s fake-service -a fake-account -w',description:'synthetic guard probe'});rows.push({file:'key-helper',error:shell.isError});}
 fs.writeFileSync(config.root+'/result.json',JSON.stringify({rows,binding:fs.existsSync(config.root+'/native-session.json')?JSON.parse(fs.readFileSync(config.root+'/native-session.json')):null,tools:ctx.tools.schemas().map(r=>r.name)}));
}catch(error){fs.writeFileSync(config.root+'/result.json',JSON.stringify({error:error.message}));}
ctx.get('appExit')(0);});}`);
      const disabled = ['session-title-llm', 'session-log-deepseek', 'plugin-package-inventory-deepseek', 'session-telemetry-otel'];
      const patchFile = save('native.patch.json', JSON.stringify([
        ...disabled.map(id => ({id, disabled: true})),
        {insert: [{id: 'orrery-tools', name: plugin, config: {cwd: root, state_dir: root, hooks_dir: hooks, env: {}}},
          {id: 'native-probe', name: probe, config: {root, mode, protected: protectedFile, helper: secretHelper}}]},
      ]));
      const offline = save('offline.mjs', "globalThis.fetch=()=>{throw new Error('NETWORK_FORBIDDEN')};");
      const result = spawnSync(nativeNode, ['--import', offline, path.join(nativeRuntime, 'node_modules/@deepseek-ai/dsh/lib/bin.js'), '--profile', 'probe', '--patch', patchFile], {
        cwd: root, env: {HOME: root, DSH_HOME: home, PATH: path.dirname(nativeNode) + ':/usr/bin:/bin', LANG: 'en_US.UTF-8', DSH_TELEMETRY_DISABLED: '1',
          AGENTSTACK_LLM_KEY_DEFINITIONS: definitions, AGENTSTACK_LLM_KEY_FILES: JSON.stringify([protectedFile])}, encoding: 'utf8', timeout: 45000});
      assert.equal(result.error, undefined, result.stdout + result.stderr);
      assert.equal(result.status, 0, result.stdout + result.stderr);
      assert.equal((result.stdout + result.stderr).includes('ORRERY_PUBLIC_HOOK_WARNING'), mode === 'loaded');
      const evidence = JSON.parse(fs.readFileSync(path.join(root, 'result.json')));
      assert.equal(evidence.error, undefined, JSON.stringify(evidence) + result.stdout + result.stderr);
      assert.ok(evidence.tools.includes('write'));
      for (const file of ['unreserved.txt', 'expired.txt', 'conflict.txt'])
        assert.equal(fs.existsSync(path.join(root, file)), mode === 'broken', JSON.stringify(evidence));
      assert.equal(fs.readFileSync(path.join(root, 'allowed.txt'), 'utf8'), 'native write');
      if (mode === 'loaded') {
        assert.equal(evidence.binding.native_session_id, 'native-own-session');
        assert.equal(evidence.binding.harness, 'dsh');
        assert.equal(fs.existsSync(path.join(root, 'helper-calls')), false);
        assert.equal(evidence.rows.find(row => row.file === 'protected-read').error, true);
        assert.equal(evidence.rows.find(row => row.file === 'key-helper').error, true);
        for (const file of ['unreserved.txt', 'expired.txt', 'conflict.txt'])
          assert.equal(evidence.rows.find(row => row.file === file).denial_visible, true);
      }
      console.log(JSON.stringify({mode, native_tool_dispatch: true, ...evidence}));
    } finally {fs.rmSync(root, {recursive: true, force: true});}
  }
});
