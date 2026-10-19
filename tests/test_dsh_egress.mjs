// All provider functions are in-memory fixtures. No sockets or credentials.
import assert from 'node:assert/strict';
import {test, after} from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import cp from 'node:child_process';
import {fileURLToPath} from 'node:url';
const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const fixtureSpawn = cp.spawn.bind(cp);
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-egress-fixture-'));
const templateFile = path.join(project, 'integrations/dsh/backend.json');
const config = JSON.parse(fs.readFileSync(templateFile));
config.backends.deepseek.base_url = 'https://deepseek.invalid/anthropic';
config.backends.strata.base_url = 'https://strata.invalid';
const configFile = path.join(root, 'fixture-backends.json');
fs.writeFileSync(configFile, JSON.stringify(config));
const dummy = 'dummy-offline-fixture-value';
Object.assign(process.env, {ORRERY_DSH_STATE_ROOT: root, ORRERY_DSH_AUDIT_FILE: path.join(root, 'audit.jsonl'),
  ORRERY_DSH_BACKEND_FILE: configFile, ORRERY_DSH_BACKEND_ID: 'strata', ORRERY_DSH_ATTEMPT_ID: 'fixture',
  ORRERY_DSH_OFFLINE: '1', DEEPSEEK_API_KEY: dummy});
fs.writeFileSync(path.join(root, 'attempt.json'), JSON.stringify({attempt_id: 'fixture', phase: 'run'}));
let spawned;
cp.spawn = (command, args, options) => { spawned = {command, args, options}; return {fixture: true}; };
const {createEgress} = await import('../integrations/dsh/egress.mjs');
const {assertIdle} = await import('../integrations/dsh/strata_status.mjs');
after(() => fs.rmSync(root, {recursive: true, force: true}));
const encode = value => new TextEncoder().encode(value);
const usage = 'data: {"type":"message_delta","usage":{"input_tokens":10,"cache_read_input_tokens":2,"output_tokens":1}}\n\n';
const endpoint = backend => backend.base_url + '/v1/messages';
const request = (backend, changes = {}) => ({method: 'POST', body: JSON.stringify({model: backend.model,
  max_tokens: 32, tools: config.wire_tools.map(name => ({name})), ...changes})});
function harness(id = 'strata', options = {}) {
  const events = [], calls = [], backend = config.backends[id];
  const fetchImpl = async (input, init) => {
    calls.push({input: String(input), init});
    if (String(input).endsWith('/v1/status')) return new Response('{"activity":{"in_flight":0}}');
    return new Response(usage);
  };
  const fetch = createEgress({backend, limits: config.limits, wireTools: config.wire_tools,
    offline: false, fetchImpl, audit: row => events.push(row), key: dummy, ...options});
  return {backend, events, calls, fetch};
}

test('both selected backends reach only their in-memory endpoint and report usage', async () => {
  for (const id of ['deepseek', 'strata']) {
    const h = harness(id);
    await (await h.fetch(endpoint(h.backend), request(h.backend))).text();
    assert.deepEqual(h.calls.map(row => row.input), id === 'strata'
      ? [h.backend.base_url + '/v1/status', endpoint(h.backend)] : [endpoint(h.backend)]);
    assert.ok(h.calls.every(row => row.init.redirect === 'manual'));
    const response = h.events.find(row => row.phase === 'provider-response');
    assert.deepEqual(response.usage, {input: 10, cache_read: 2, output: 1});
    assert.ok(response.duration_ms >= 0);
    assert.equal(h.events.at(-1).phase, 'stream-end');
  }
});

test('offline captures assembled tools with no provider or status call', async () => {
  const h = harness('strata', {offline: true});
  await assert.rejects(h.fetch(endpoint(h.backend), request(h.backend)), /DSH_OFFLINE/);
  assert.equal(h.calls.length, 0);
  assert.equal(h.events.at(-1).forwarded, 0);
});

test('spike accepts read/write only but remains offline even if caller asks for transport', async () => {
  const h = harness('strata', {phase: 'spike', offline: false});
  await assert.rejects(h.fetch(endpoint(h.backend), request(h.backend, {tools: [{name: 'read'}, {name: 'write'}]})), /DSH_OFFLINE/);
  assert.equal(h.calls.length, 0);
});

test('balance, npm, GitHub and arbitrary loopback are blocked for both backends', async () => {
  for (const id of ['deepseek', 'strata']) for (const url of ['https://api.deepseek.com/user/balance',
    'https://registry.npmjs.org/pkg', 'https://api.github.com/repos/x/y', 'http://127.0.0.1:12345/']) {
    const h = harness(id);
    await assert.rejects(h.fetch(url, request(h.backend)), /DSH_NETWORK/);
    assert.equal(h.calls.length, 0);
    assert.equal(h.events.at(-1).phase, 'blocked-network');
  }
});

test('unknown model, unsupported beta, structural tool_addition and extra tools do not forward', async () => {
  for (const change of [{model: 'other'}, {max_tokens: 100000},
    {messages: [{content: [{type: 'tool_addition'}]}]}, {tools: [{name: 'bash'}]}]) {
    const h = harness();
    await assert.rejects(h.fetch(endpoint(h.backend), request(h.backend, change)));
    assert.equal(h.calls.length, 0);
  }
  const h = harness();
  await assert.rejects(h.fetch(endpoint(h.backend), {...request(h.backend), headers: {'anthropic-beta': 'mid-conversation-tool-changes'}}));
  assert.equal(h.calls.length, 0);
  // Literal documentation text is not a protocol tool_addition block.
  const normal = harness();
  await (await normal.fetch(endpoint(normal.backend), request(normal.backend,
    {messages: [{content: 'Explain tool_addition in a document'}]}))).text();
});

test('Strata busy, unknown, auth and 404 health fallback are handled without polling', async () => {
  for (const body of ['{"in_flight":1}', '{"status":"ok"}', '{"in_flight":0,"queued":1}']) {
    let count = 0;
    await assert.rejects(assertIdle(async () => { count++; return new Response(body); },
      'https://fixture.invalid', dummy, () => {}));
    assert.equal(count, 1);
  }
  let calls = [];
  await assertIdle(async (url, init) => { calls.push({url, init}); return calls.length === 1
    ? new Response('', {status: 401}) : new Response('{"in_flight":0}'); },
  'https://fixture.invalid', dummy, () => {});
  assert.equal(calls.length, 2);
  assert.equal(calls[0].init.headers, undefined);
  assert.equal(calls[1].init.headers.Authorization, 'Bearer ' + dummy);
  calls = [];
  await assertIdle(async url => { calls.push(String(url)); return calls.length === 1
    ? new Response('', {status: 404}) : new Response('{"in_flight":0}'); },
  'https://fixture.invalid', dummy, () => {});
  assert.equal(calls.at(-1), 'https://fixture.invalid/health');
});

test('redirect responses are not followed by provider or status', async () => {
  const h = harness('deepseek', {fetchImpl: async () => new Response('', {status: 307, headers: {location: 'https://not-allowed.invalid'}})});
  await assert.rejects(h.fetch(endpoint(h.backend), request(h.backend)), error => error.dshExitCode === 78);
  assert.ok(h.events.some(row => row.phase === 'blocked-redirect'));
  const events = [];
  await assert.rejects(assertIdle(async () => new Response('', {status: 302}),
    'https://fixture.invalid', dummy, row => events.push(row)), error => error.dshExitCode === 78);
  assert.ok(events.some(row => row.phase === 'blocked-redirect'));
});

test('one in-flight request is held through body EOF, then request cap stops the next', async () => {
  const h = harness('strata', {limits: {...config.limits, session_requests: 1}});
  const response = await h.fetch(endpoint(h.backend), request(h.backend));
  await assert.rejects(h.fetch(endpoint(h.backend), request(h.backend)), /IN_FLIGHT/);
  assert.equal(h.calls.filter(row => row.input.endsWith('/v1/messages')).length, 1);
  await response.text();
  await assert.rejects(h.fetch(endpoint(h.backend), request(h.backend)), /REQUEST_CAP/);
});

test('stream cancellation releases the local slot but unknown usage blocks a new request', async () => {
  const h = harness('deepseek', {fetchImpl: async () => new Response(new ReadableStream({pull() {}}))});
  const response = await h.fetch(endpoint(h.backend), request(h.backend));
  await response.body.cancel();
  assert.equal(h.events.at(-1).ok, false);
  await assert.rejects(h.fetch(endpoint(h.backend), request(h.backend)), /USAGE_UNKNOWN/);
});

test('transport timeout and stream error cannot produce a clean stream receipt', async () => {
  const h = harness('deepseek', {fetchImpl: async (url, {signal}) => new Response(new ReadableStream({
    start(controller) { signal.addEventListener('abort', () => controller.error(new Error('fixture timeout'))); }
  })), limits: {...config.limits, max_request_ms: 15}});
  const hold = setTimeout(() => {}, 100);
  try {
    await assert.rejects((await h.fetch(endpoint(h.backend), request(h.backend))).text(), /DSH_STREAM/);
    assert.equal(h.events.at(-1).ok, false);
  } finally { clearTimeout(hold); }
});

test('a provider cannot execute a tool outside the wire allowlist', async () => {
  const h = harness('deepseek', {fetchImpl: async () => new Response('data: {"content_block":{"type":"tool_use","name":"bash"}}\n\n')});
  await assert.rejects((await h.fetch(endpoint(h.backend), request(h.backend))).text());
  assert.ok(h.events.some(row => row.phase === 'blocked-model-tool'));
});

test('proxy child receives only scrubbed env and auxiliary commands are blocked', () => {
  const proxy = path.join(os.homedir(), '.agentstack/integrations/codex_app/plugin/scripts/run-mcp.sh');
  cp.spawn(proxy, [], {env: {HOME: os.homedir(), DEEPSEEK_API_KEY: dummy, OTHER_SECRET: dummy,
    AGENTSTACK_PROXY_AGENT_NAME: 'Fixture'}});
  assert.equal(spawned.options.env.DEEPSEEK_API_KEY, undefined);
  assert.equal(spawned.options.env.OTHER_SECRET, undefined);
  assert.equal(spawned.options.env.AGENTSTACK_PROXY_AGENT_NAME, 'Fixture');
  assert.throws(() => cp.spawn('/usr/bin/curl', []), /DSH_CHILD/);
  assert.throws(() => cp.execFile('/usr/bin/false'), /DSH_AUXILIARY/);
});

test('product-generated proxy config reaches a real offline child with its identity and no provider key', async () => {
  // No real proxy, identity, credential file or socket: only the generator is real.
  const dir = fs.mkdtempSync(path.join(root, 'spawn-'));
  const home = path.join(dir, 'home');
  const proxy = path.join(home, '.agentstack/integrations/codex_app/plugin/scripts/run-mcp.sh');
  fs.mkdirSync(path.dirname(proxy), {recursive: true});
  fs.writeFileSync(proxy, '#!' + process.execPath + '\n' +
    'console.log(JSON.stringify({names:Object.keys(process.env),agent:process.env.AGENTSTACK_PROXY_AGENT_NAME,' +
    'program:process.env.AGENTSTACK_PROXY_PROGRAM,parent:process.env.AGENTSTACK_PROXY_PARENT_AGENT}));\n', {mode: 0o700});
  const claudeFile = path.join(dir, 'empty.json'), configFile = path.join(dir, 'mcp.json');
  fs.writeFileSync(claudeFile, '{"mcpServers":{},"projects":{}}');
  const env = {PATH: '/usr/bin:/bin', HOME: home};
  const run = (command, args, options) => new Promise((resolve, reject) => {
    const child = fixtureSpawn(command, args, {...options, stdio: ['ignore', 'pipe', 'pipe']});
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.once('error', reject);
    child.once('close', code => resolve({code, stdout, stderr}));
  });
  const generated = await run(process.env.ORRERY_DSH_TEST_PYTHON ?? '/usr/bin/python3', [
    path.join(project, 'hooks/child_tools.py'), 'claude-config', '--spec', '{"base":"mail-only","tools":{}}',
    '--cwd', dir, '--chrome', '0', '--claude-json', claudeFile, '--out', configFile,
    '--runner', proxy, '--child', 'FixtureChild', '--project-key', dir,
    '--token-file', path.join(dir, 'not-a-real-token'), '--mcp-url', 'http://127.0.0.1:1/mcp',
    '--mail-env', path.join(dir, 'not-a-real-mail-env'), '--runtime-dir', dir,
    '--bearer-mode', 'disabled', '--python-bin', '/usr/bin/python3', '--parent-agent', 'FixtureParent'
  ], {env});
  assert.equal(generated.code, 0, generated.stderr);
  fs.writeFileSync(path.join(dir, 'attempt.json'), JSON.stringify({attempt_id: 'fixture-spawn', phase: 'run'}));
  const entry = path.join(dir, 'fixture-entry.mjs');
  fs.writeFileSync(entry, `import fs from 'node:fs'; import {spawn} from 'node:child_process';
    const proxy=JSON.parse(fs.readFileSync(${JSON.stringify(configFile)},'utf8')).mcpServers['orrery-mail'];
    const child=spawn(proxy.command,proxy.args,{env:{...process.env,...proxy.env},stdio:['ignore','pipe','inherit']});
    child.stdout.pipe(process.stdout); child.on('exit',code=>process.exitCode=code);
  `);
  const executed = await run(process.execPath, ['--import', path.join(project, 'integrations/dsh/egress.mjs'), entry], {
    env: {...env, ORRERY_DSH_STATE_ROOT: dir, ORRERY_DSH_AUDIT_FILE: path.join(dir, 'audit.jsonl'),
      ORRERY_DSH_BACKEND_FILE: path.join(root, 'fixture-backends.json'),
      ORRERY_DSH_BACKEND_ID: 'strata', ORRERY_DSH_ATTEMPT_ID: 'fixture-spawn', ORRERY_DSH_OFFLINE: '1',
      DEEPSEEK_API_KEY: dummy, OTHER_SECRET: dummy}
  });
  assert.equal(executed.code, 0, executed.stderr);
  const report = JSON.parse(executed.stdout);
  assert.equal(report.agent, 'FixtureChild');
  assert.equal(report.parent, 'FixtureParent');
  assert.equal(report.program, 'claude-code'); // Preserve the product generator's value.
  assert.ok(!report.names.includes('DEEPSEEK_API_KEY') && !report.names.includes('OTHER_SECRET'));
  for (const key of Object.keys(JSON.parse(fs.readFileSync(configFile)).mcpServers['orrery-mail'].env))
    assert.ok(report.names.includes(key), key);
  const rows = fs.readFileSync(path.join(dir, 'audit.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(rows.filter(row => row.phase === 'mcp-child-env').length, 1);
  assert.ok(!rows.some(row => ['provider-request-start','status-check'].includes(row.phase)));
});

test('native startup diagnostic is redacted before disk write', () => {
  const log = path.join(root, 'startup-fixture.log');
  fs.writeFileSync(log, 'fixture header value ' + dummy);
  assert.ok(!fs.readFileSync(log, 'utf8').includes(dummy));
  assert.ok(fs.readFileSync(log, 'utf8').includes('[REDACTED]'));
});
