// Real native CLI, private HOME, synthetic security, no provider/network traffic.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
import {spawn, spawnSync} from 'node:child_process';
import http from 'node:http';
import {once} from 'node:events';
import {providerClass, secretServiceLookup} from '../integrations/dsh/keychain_credentials.mjs';

// Exercise the production function verbatim on any CI host, without a store.
function linuxReaderProbe(execFile) {
  const raw = fs.readFileSync(new URL('../integrations/dsh/keychain_credentials.mjs', import.meta.url), 'utf8');
  const start = raw.indexOf('export function secretServiceLookup(');
  const end = raw.indexOf('export function keychainLookup(', start);
  assert.ok(start >= 0 && end > start);
  const sandbox = {path, execFile, process: {platform: 'linux', env: {
    HOME: '/synthetic/home', PATH: '/synthetic/bin', LANG: 'C.UTF-8',
    DBUS_SESSION_BUS_ADDRESS: 'unix:path=/synthetic/bus', XDG_RUNTIME_DIR: '/synthetic/runtime',
    DEEPSEEK_API_KEY: 'synthetic-env-canary', NODE_OPTIONS: '--import forbidden',
  }}};
  vm.createContext(sandbox);
  vm.runInContext(raw.slice(start, end).replace(/^export /, '') + '\nthis.lookup = secretServiceLookup;', sandbox);
  return sandbox.lookup;
}

function readerCommand(connection) {
  return ['/synthetic/python', '-I', '-B', '/synthetic/hooks/llm_secret_reader.py',
    '--manifest', '/synthetic/manifest.json', '--backend', connection];
}

test('secret service reader preserves legacy and additional connection IDs through execFile', async t => {
  const calls = [];
  const lookup = linuxReaderProbe((file, args, options, callback) => {
    calls.push({file, args, options}); callback(null, 'synthetic-memory-value\r\n');
  });
  for (const connection of ['deepseek', 'strata', 'strata-epyc']) {
    const command = readerCommand(connection);
    const before = calls.length;
    let value;
    try {value = await lookup({reader_command: command});}
    finally {t.diagnostic(JSON.stringify({connection, exec_calls: calls.length - before,
      ...(calls.length > before ? {call: calls.at(-1)} : {})}));}
    assert.equal(value, 'synthetic-memory-value');
    const call = calls.at(-1);
    assert.equal(call.file, command[0]);
    assert.deepEqual(call.args, command.slice(1));
    assert.equal(call.options.timeout, 8000);
    assert.equal(call.options.maxBuffer, 65536);
    assert.equal(call.options.encoding, 'utf8');
    assert.deepEqual({...call.options.env}, {HOME: '/synthetic/home', PATH: '/synthetic/bin', LANG: 'C.UTF-8',
      DBUS_SESSION_BUS_ADDRESS: 'unix:path=/synthetic/bus', XDG_RUNTIME_DIR: '/synthetic/runtime'});
  }
  assert.equal(calls.length, 3);
});

test('secret service reader rejects malformed commands and IDs without execFile', async () => {
  let calls = 0;
  const lookup = linuxReaderProbe(() => {calls++; throw Error('helper must not execute');});
  const valid = readerCommand('strata-epyc');
  const changed = (index, value) => valid.map((item, position) => position === index ? value : item);
  const commands = [null, {}, valid.slice(1), [...valid, 'extra'],
    changed(0, 'relative-python'), changed(0, 42), changed(1, '-c'), changed(2, '-E'),
    changed(3, 'relative/llm_secret_reader.py'), changed(3, '/synthetic/wrong.py'),
    changed(4, '--other'), changed(5, 'relative.json'), changed(6, '--other')];
  for (const id of ['', 'Strata-epyc', ' strata-epyc', 'strata-epyc\n', '../strata', 'strata/epyc',
                    '-strata', 'strata.epyc', 's'.repeat(65), null, 7, {toString: () => 'strata-epyc'}]) {
    commands.push(changed(7, id));
  }
  for (const reader_command of commands) {
    await assert.rejects(async () => lookup({reader_command}), /^Error: DSH_CREDENTIAL_REFERENCE_INVALID$/);
  }
  assert.equal(calls, 0);
  for (const [error, output] of [[Error('synthetic-private-stderr'), 'synthetic-private-value'], [null, ' \n']]) {
    const failed = linuxReaderProbe((_file, _args, _options, callback) => callback(error, output));
    await assert.rejects(failed({reader_command: valid}), /^Error: DSH_CREDENTIAL_UNAVAILABLE$/);
  }
});

test('secret service reader leaves undeclared connection rejection to the real Python reader', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ss-declaration-'));
  try {
    const manifest = path.join(root, 'manifest.json');
    fs.writeFileSync(manifest, JSON.stringify({backends: {strata: {credential_ref: null}}}), {mode: 0o600});
    const located = spawnSync(process.env.ORRERY_DSH_TEST_PYTHON ?? 'python3',
      ['-I', '-B', '-c', 'import sys;print(sys.executable)'], {encoding: 'utf8'});
    assert.equal(located.status, 0, located.stderr);
    const python = located.stdout.trim(); assert.ok(path.isAbsolute(python));
    const command = [python, '-I', '-B', path.resolve('hooks/llm_secret_reader.py'),
      '--manifest', manifest, '--backend', 'strata-absent'];
    let calls = 0, result;
    const lookup = linuxReaderProbe((file, args, options, callback) => {
      calls++; assert.equal(file, python); assert.deepEqual(args, command.slice(1));
      // Import the actual reader/main; fail the test if it reaches key lookup.
      const probe = 'import runpy,sys\nreader=runpy.run_path(sys.argv[1])\n' +
        'def forbidden(_):raise AssertionError("REAL_KEY_LOOKUP_FORBIDDEN")\n' +
        'reader["main"].__globals__["read_ref"]=forbidden\nsys.argv=sys.argv[1:]\nraise SystemExit(reader["main"]())\n';
      result = spawnSync(file, ['-I', '-B', '-c', probe, ...args.slice(2)],
        {encoding: 'utf8', timeout: options.timeout, env: {HOME: root, PATH: options.env.PATH, LANG: options.env.LANG}});
      callback(result.status === 0 ? null : Error('reader failed'), result.stdout ?? '');
    });
    await assert.rejects(lookup({reader_command: command}), /^Error: DSH_CREDENTIAL_UNAVAILABLE$/);
    assert.equal(calls, 1); assert.equal(result.status, 1);
    assert.equal(result.stdout, ''); assert.equal(result.stderr, 'invalid_credential_ref\n');
    t.diagnostic(JSON.stringify({connection: command.at(-1), exec_calls: calls,
      reader_rc: result.status, stdout: result.stdout, stderr: result.stderr}));
  } finally { fs.rmSync(root, {recursive: true, force: true}); }
});

test('secret service reader keeps selected value in memory and failure cannot fall back', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ss-reader-'));
  const original = Object.getOwnPropertyDescriptor(process, 'platform');
  try {
    Object.defineProperty(process, 'platform', {value: 'linux'});
    const python = path.join(root, 'synthetic-python');
    fs.writeFileSync(python, '#!/bin/sh\nprintf "synthetic-ss-value\\n"\n', {mode: 0o700});
    const config = {ref: 'selected', reader_command: [python, '-I', '-B', path.join(root, 'llm_secret_reader.py'),
      '--manifest', path.join(root, 'manifest.json'), '--backend', 'deepseek']};
    let calls = 0;
    const Provider = providerClass(Local, {init: INIT}, config, async value => {calls++; return secretServiceLookup(value);});
    const provider = new Provider({});
    assert.equal((await provider.resolve('selected')).value, 'synthetic-ss-value');
    assert.equal((await provider.resolve('selected')).value, 'synthetic-ss-value');
    assert.equal(calls, 1);
    await assert.rejects(provider.write('selected', 'private'), /READ_ONLY/);
    fs.writeFileSync(python, '#!/bin/sh\nexit 1\n');
    const failed = new Provider({});
    await assert.rejects(failed.resolve('selected'), /UNAVAILABLE/);
    assert.equal(Local.calls, 0);
    await assert.rejects(secretServiceLookup({...config, reader_command: ['/bin/sh', '-c', 'forbidden']}), /INVALID/);
  } finally {
    Object.defineProperty(process, 'platform', original);
    fs.rmSync(root, {recursive: true, force: true});
  }
});

const INIT = Symbol('init');
class Local {
  static calls = 0;
  async *[INIT]() {}
  async resolve() {Local.calls++; return {value: 'ambient-canary'};}
  async describe() {return {writable: true};}
  async write() {throw new Error('should not persist selected ref');}
}

test('one memory lookup, no selected env/file fallback or persistence, failure cached', async () => {
  let calls = 0;
  const Provider = providerClass(Local, {init: INIT}, {ref: 'selected'}, async () => {calls++; return 'memory-fixture';});
  const p = new Provider({});
  assert.equal((await p.resolve('selected')).value, 'memory-fixture');
  assert.equal((await p.resolve('selected')).value, 'memory-fixture');
  assert.equal(calls, 1);
  assert.equal(Local.calls, 0);
  await assert.rejects(p.write('selected', 'anything'), /READ_ONLY/);
  assert.equal((await p.describe('selected')).writable, false);
  await p.resolve('other');
  assert.equal(Local.calls, 1);
  calls = 0;
  const Bad = providerClass(Local, {init: INIT}, {ref: 'selected'}, async () => {calls++; throw new Error('private stderr');});
  const bad = new Bad({});
  for (let i = 0; i < 2; i++) await assert.rejects(bad.resolve('selected'), /^Error: DSH_KEYCHAIN_UNAVAILABLE$/);
  assert.equal(calls, 1);
});

const runtime = process.env.ORRERY_DSH_TEST_RUNTIME;
const node = process.env.ORRERY_DSH_TEST_NODE;
const nativeSkip = !runtime || !node ? 'explicit pinned native runtime and Node required' : false;
const plugin = pathToFileURL(path.resolve('integrations/dsh/keychain_credentials.mjs')).href;

function nativeCase(mode) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-native-key-'));
  const save = (name, data) => {const file = path.join(root, name); fs.mkdirSync(path.dirname(file), {recursive: true}); fs.writeFileSync(file, data, {mode: 0o600}); return file;};
  const profile = path.join(root, 'dsh/profiles/probe');
  save('dsh/profiles/probe/package.json', JSON.stringify({name: 'offline-probe', private: true, dsh: {profile: {bundles: []}}}));
  fs.symlinkSync(path.join(runtime, 'node_modules'), path.join(profile, 'node_modules'));
  save('dsh/profiles/probe/cordis.patch.yml', '[]');
  const selected = 'ORRERY_DSH_PROVIDER_KEY';
  save('dsh/.credentials.yaml', 'version: 1\nrefs:\n  ' + selected + ': "FILE_CANARY"\n  DEEPSEEK_API_KEY: "OTHER_FILE_CANARY"\n');
  const originalCredentials = fs.readFileSync(path.join(root, 'dsh/.credentials.yaml'), 'utf8');
  const fakeSecurity = save('security.mjs', `import fs from 'node:fs';fs.appendFileSync(${JSON.stringify(path.join(root, 'security-calls'))}, 'lookup\\n');${mode === 'lookup-failure' ? 'process.exit(1);' : "process.stdout.write('MEMORY_FIXTURE_KEY');"}`);
  const credentialWrapper = save('credential-wrapper.mjs', `import {execFile} from 'node:child_process';import {install} from ${JSON.stringify(plugin)};
export async function apply(ctx, config) { await install(ctx, config, () => new Promise((resolve,reject)=>execFile(${JSON.stringify(node)},[${JSON.stringify(fakeSecurity)}],{encoding:'utf8'},(err,out)=>err?reject(err):resolve(out)))); }`);
  const probe = save('probe.mjs', `import fs from 'node:fs';export function apply(ctx, config) {
ctx.get('appReady').onReady(async () => { const out = {native_process:true, credentials_loaded:!!ctx.get('credentials'), adapter_loaded:ctx.get('llm').adapters.has('deepseek-official'), auth_ok:false, canary_used:false, network_requests:globalThis.__networkRequests??0};
try { const adapter = ctx.get('llm').adapters.get('deepseek-official')?.adapter;
if(adapter) { const auth = await adapter.dependencies.resolveAuth({apiKeyEnv:${JSON.stringify(selected)}}); out.auth_ok=auth.headers['x-api-key']==='MEMORY_FIXTURE_KEY';out.canary_used=auth.headers['x-api-key'].includes('CANARY'); }
const creds=ctx.get('credentials'); if(creds && out.auth_ok) {out.memory_twice=(await creds.resolve(${JSON.stringify(selected)})).value==='MEMORY_FIXTURE_KEY';try {await creds.set(${JSON.stringify(selected)},'persist-attempt');}catch{out.write_denied=true;} }
} catch(error) {out.auth_failed=true;out.error_type=error.name;out.error_message=error.message;}
fs.writeFileSync(config.result,JSON.stringify(out));ctx.get('appExit')(0); }); }`);
  const guard = save('offline.mjs', "globalThis.__networkRequests=0;globalThis.fetch=()=>{globalThis.__networkRequests++;throw new Error('NETWORK_FORBIDDEN')};");
  const rows = [
    {id: 'llm', name: '@deepseek-ai/dsh-llm'},
    {id: 'credentials', name: mode === 'missing' ? path.join(root, 'absent.mjs') : mode === 'import-failure' ? save('broken.mjs', 'throw new Error("BROKEN_PLUGIN")') : credentialWrapper,
      disabled: mode === 'disabled', config: {runtime, dshHome: path.join(root, 'dsh'), ref: selected, service: 'fake-service', account: 'fake-account'}},
    {id: 'llm-provider', name: '@deepseek-ai/dsh-llm-deepseek-api-key', inject: ['llm', 'credentials'], config: {baseURL: 'https://provider.invalid', apiKeyEnv: selected}},
    {id: 'probe', name: probe, inject: ['llm'], config: {result: path.join(root, 'result.json')}},
  ];
  save('patch.json', JSON.stringify([{insert: rows}]));
  const result = spawnSync(node, ['--import', guard, path.join(runtime, 'node_modules/@deepseek-ai/dsh/lib/bin.js'), '--profile', 'probe', '--patch', path.join(root, 'patch.json')],
    {cwd: root, env: {HOME: root, DSH_HOME: path.join(root, 'dsh'), PATH: path.dirname(node) + ':/usr/bin:/bin', LANG: 'en_US.UTF-8',
      DEEPSEEK_API_KEY: 'ENV_CANARY', ORRERY_DSH_PROVIDER_KEY: 'SELECTED_ENV_CANARY', DSH_TELEMETRY_DISABLED: '1'}, encoding: 'utf8', timeout: 30000});
  let evidence;
  try {
    if (fs.existsSync(path.join(root, 'result.json'))) evidence = JSON.parse(fs.readFileSync(path.join(root, 'result.json')));
    const calls = fs.existsSync(path.join(root, 'security-calls')) ? fs.readFileSync(path.join(root, 'security-calls'), 'utf8').trim().split('\n').length : 0;
    assert.equal(result.error, undefined, 'native CLI timed out: ' + result.stdout + result.stderr);
    assert.equal(fs.readFileSync(path.join(root, 'dsh/.credentials.yaml'), 'utf8'), originalCredentials);
    assert.equal((result.stdout + result.stderr).includes('MEMORY_FIXTURE_KEY'), false);
    assert.equal((result.stdout + result.stderr).includes('SELECTED_ENV_CANARY'), false);
    if (mode === 'ok') {
      assert.equal(result.status, 0, result.stderr);
      assert.ok(evidence?.credentials_loaded, JSON.stringify(evidence) + result.stdout + result.stderr);
      assert.ok(evidence.auth_ok, JSON.stringify(evidence) + result.stdout + result.stderr);
      assert.ok(evidence.memory_twice);
      assert.ok(evidence.write_denied);
      assert.equal(calls, 1);
      assert.equal(evidence.network_requests, 0);
    } else {
      assert.equal(evidence?.network_requests ?? 0, 0);
      assert.equal(evidence?.canary_used ?? false, false);
      assert.equal(evidence?.auth_ok ?? false, false);
      assert.equal(calls, mode === 'lookup-failure' ? 1 : 0);
      assert.ok(evidence || result.status !== 0, 'negative did not run probe or fail visibly: ' + result.stderr);
    }
    return {mode, exit: result.status, evidence, security_calls: calls};
  } finally { fs.rmSync(root, {recursive: true, force: true}); }
}

test('native CLI loads memory credentials; absent/disabled/import/lookup cannot fall back to canaries', {skip: nativeSkip}, () => {
  for (const mode of ['ok', 'missing', 'disabled', 'import-failure', 'lookup-failure']) {
    const row = nativeCase(mode);
    console.log(JSON.stringify(row));
  }
});

for (const mode of ['ok', 'missing', 'lookup-failure', 'working-enabled']) test(`generated base profile HTTP credential path: ${mode}`, {skip: nativeSkip}, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-generated-key-'));
  const save = (name, data) => {
    const file = path.join(root, name); fs.mkdirSync(path.dirname(file), {recursive: true});
    fs.writeFileSync(file, typeof data === 'string' ? data : JSON.stringify(data), {mode: 0o600}); return file;
  };
  const sse = [
    {type: 'message_start', message: {id: 'fixture', type: 'message', role: 'assistant', content: [], model: 'deepseek-flash', usage: {input_tokens: 1, output_tokens: 0}}},
    {type: 'content_block_start', index: 0, content_block: {type: 'text', text: ''}},
    {type: 'content_block_delta', index: 0, delta: {type: 'text_delta', text: 'fixture-ok'}},
    {type: 'content_block_stop', index: 0},
    {type: 'message_delta', delta: {stop_reason: 'end_turn', stop_sequence: null}, usage: {output_tokens: 1}},
    {type: 'message_stop'},
  ].map(row => `event: ${row.type}\ndata: ${JSON.stringify(row)}\n\n`).join('');
  let received = [];
  const server = http.createServer(async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk;
    received.push({method: req.method, url: req.url, key: req.headers['x-api-key'], body: JSON.parse(body)});
    res.writeHead(200, {'content-type': 'text/event-stream'}); res.end(sse);
  });
  try {
    server.listen(0, '127.0.0.1'); await once(server, 'listening');
    const baseURL = `http://127.0.0.1:${server.address().port}/anthropic`;
    const state = path.join(root, 'state'); fs.mkdirSync(state);
    const receipt = save('owner.json', {harness: 'dsh', complete: true, variant: 'plain-datadir', thin_config_contract: 1,
      details: {node, runtime, runtime_receipt: path.join(runtime, '../evidence/upstream-runtime.json')}});
    const backends = save('backends.json', {backends: {deepseek: {base_url: baseURL, model: 'deepseek-flash', keychain: {service: 'fake-service', account: 'fake-account'}}}});
    const template = save('template.json', {node, runtime, generation_receipt: receipt, backend_file: backends});
    const manifest = save('manifest.json', {enabled: true, backends: {deepseek: {model: 'deepseek-flash'}}, harnesses: {dsh: {config_file: template}}});
    const document = {product_root: process.cwd(), manifest_file: manifest, backend_id: 'deepseek', model: 'deepseek-flash',
      agent_name: 'FixtureChild', cwd: root, state_dir: state, runtime_dir: root,
      prompt_file: save('prompt.txt', ''), instructions_file: save('instructions.md', 'fixture instructions'),
      mcp_config: save('mcp.json', {mcpServers: {}}), mcp_selection: {servers: {}, inherit: false, approved: {}, enabled_tools: {}}};
    const input = save('input.json', document), planFile = path.join(root, 'plan.json');
    const generated = spawnSync(process.env.ORRERY_DSH_TEST_PYTHON ?? 'python3',
      [process.env.ORRERY_DSH_TEST_GENERATOR ?? path.resolve('integrations/dsh/thin_config.py'), '--input', input, '--out', planFile], {encoding: 'utf8'});
    assert.equal(generated.status, 0, generated.stderr);
    const plan = JSON.parse(fs.readFileSync(planFile, 'utf8'));
    const home = plan.env.DSH_HOME, selected = 'ORRERY_DSH_PROVIDER_KEY';
    const credentials = save(path.relative(root, path.join(home, '.credentials.yaml')), `version: 1\nrefs:\n  ${selected}: "FILE_CANARY"\n`);
    const initialCredentials = fs.readFileSync(credentials, 'utf8');
    const fakeSecurity = save('security.mjs', `import fs from 'node:fs';fs.appendFileSync(${JSON.stringify(path.join(root, 'lookups'))},'lookup\\n');${mode === 'lookup-failure' ? 'process.exit(1);' : "process.stdout.write('MEMORY_FIXTURE_KEY');"}`);
    const wrapper = save('credential-wrapper.mjs', `import {execFile} from 'node:child_process';import {install} from ${JSON.stringify(plugin)};
export async function apply(ctx, config) {await install(ctx,config,()=>new Promise((resolve,reject)=>execFile(${JSON.stringify(node)},[${JSON.stringify(fakeSecurity)}],{encoding:'utf8'},(e,out)=>e?reject(e):resolve(out))));}`);
    const rows = JSON.parse(fs.readFileSync(path.join(plan.config_dir, 'native.patch.json'), 'utf8'));
    // Only replace the lookup seam; preserve every generated patch operation.
    const entries = rows.flatMap(row => row.insert ?? [row]);
    const keyRow = entries.find(row => row.name === path.resolve('integrations/dsh/keychain_credentials.mjs'));
    assert.ok(keyRow); keyRow.name = mode === 'missing' ? path.join(root, 'missing.mjs') : wrapper;
    // Negative control: re-enable only the entry with broken upstream deps.
    save('generated.patch.json', mode === 'working-enabled' ? rows.filter(row => row.id !== 'working-activity') : rows);
    const tuiModules = path.join(runtime, 'node_modules/@deepseek-harness-tui/dsh-tui/lib/types');
    const probe = save('http-probe.mjs', `import fs from 'node:fs';
import {resolveStartupLang} from ${JSON.stringify(pathToFileURL(path.join(tuiModules, 'i18n.js')).href)};
import {shouldOfferOnboarding} from ${JSON.stringify(pathToFileURL(path.join(tuiModules, 'onboardingPrefs.js')).href)};
export function apply(ctx) {ctx.get('appReady').onReady(async()=>{
 const out={credentials_loaded:!!ctx.get('credentials'),finished:false,language:resolveStartupLang(),wizard:shouldOfferOnboarding()};
 try {const adapter=ctx.get('llm').adapters.get('deepseek-official').adapter;
 for await(const event of adapter.stream({provider:'deepseek-official',model:'deepseek-flash',maxTokens:8,messages:[{role:'user',content:[{type:'text',text:'fixture'}]}]})) {}
 out.finished=true;
 } catch(error) {out.error=error.message;}
 fs.writeFileSync(${JSON.stringify(path.join(root, 'http-result.json'))},JSON.stringify(out));ctx.get('appExit')(0);
});}`);
    const extra = save('probe.patch.json', [{id: 'dsh-tui', disabled: true}, {insert: [{id: 'http-probe', name: probe, inject: ['llm']}]}]);
    const guard = save('local-only.mjs', `const fetch0=globalThis.fetch;globalThis.fetch=(url,opts)=>{
 if(!String(url).startsWith(${JSON.stringify(baseURL + '/')}))throw new Error('EXTERNAL_NETWORK_FORBIDDEN');return fetch0(url,opts);};`);
    const argv = [...plan.argv]; argv[argv.indexOf('--patch') + 1] = path.join(root, 'generated.patch.json');
    const child = spawn(node, ['--import', guard, ...argv.slice(1), '--patch', extra], {cwd: root,
      env: {HOME: root, PATH: path.dirname(node) + ':/usr/bin:/bin', LC_CTYPE: 'ja_JP.UTF-8', ...plan.env,
        DEEPSEEK_API_KEY: 'ENV_CANARY', ORRERY_DSH_PROVIDER_KEY: 'SELECTED_ENV_CANARY', DSH_TELEMETRY_DISABLED: '1'}, timeout: 30000});
    let output = ''; child.stdout.on('data', x => {output += x;}); child.stderr.on('data', x => {output += x;});
    const [exit] = await once(child, 'close');
    assert.equal(exit, 0, output);
    if (mode === 'working-enabled') {
      assert.match(output, /working-activity[\s\S]*failed to import/);
      assert.match(output, /ERR_MODULE_NOT_FOUND[\s\S]*@deepseek-ai\/schemastery/);
      assert.match(output, /did not activate/);
    } else if (mode === 'ok') {
      assert.equal(output.includes('did not activate'), false, output);
      assert.equal(output.includes('failed to import'), false, output);
    }
    const evidence = JSON.parse(fs.readFileSync(path.join(root, 'http-result.json'), 'utf8'));
    assert.equal(evidence.language, 'en'); assert.equal(evidence.wizard, false);
    if (mode === 'ok' || mode === 'working-enabled') {
      assert.equal(evidence.finished, true, JSON.stringify(evidence) + output);
      assert.equal(received.length, 1);
      assert.equal(received[0].method, 'POST'); assert.equal(received[0].url, '/anthropic/v1/messages');
      assert.equal(received[0].key, 'MEMORY_FIXTURE_KEY');
      assert.equal(received[0].body.model, 'deepseek-flash');
    } else {
      assert.equal(evidence.finished, false); assert.equal(received.length, 0);
      assert.equal(evidence.credentials_loaded, false);
    }
    assert.equal(fs.existsSync(path.join(root, 'lookups')) ? fs.readFileSync(path.join(root, 'lookups'), 'utf8') : '', mode === 'missing' ? '' : 'lookup\n');
    assert.equal(fs.readFileSync(credentials, 'utf8'), initialCredentials);
    assert.equal(/MEMORY_FIXTURE_KEY|SELECTED_ENV_CANARY|FILE_CANARY/.test(output), false);
    console.log(JSON.stringify({mode, generated_profile: true, native_base: true, local_http_requests: received.length,
      memory_header: mode === 'ok' || mode === 'working-enabled', lookup_count: mode === 'missing' ? 0 : 1,
      language: evidence.language, wizard: evidence.wizard, startup_warning: output.includes('did not activate'),
      startup_head: output.split('\n').filter(Boolean).slice(0, 3)}));
  } finally {
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    fs.rmSync(root, {recursive: true, force: true});
  }
});
