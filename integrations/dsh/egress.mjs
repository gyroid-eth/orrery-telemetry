// Startup/auxiliary calls happen in normal use and can leak keys or live data.
// Derived from the successful 2026-10-19 headless kit. No proxy/firewall here.
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import cp from 'node:child_process';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {syncBuiltinESMExports} from 'node:module';
import {assertIdle} from './strata_status.mjs';

export function createEgress({backend, limits, wireTools, offline = true, phase = 'run',
  fetchImpl, audit, key = '', fail = (reason, code) => { throw Object.assign(new Error(reason), {dshExitCode: code}); }}) {
  const endpoint = new URL(backend.base_url.replace(/\/+$/, '') + '/v1/messages');
  const allow = new Set(wireTools);
  const required = phase === 'spike' ? ['read', 'write'] : wireTools;
  if (phase === 'spike') offline = true;
  let seq = 0, active = false, usageKnown = true;
  const reject = (phase, reason, extra = {}) => {
    audit({phase, ...extra});
    return fail(reason, phase === 'limit-reached' ? 80 : 78);
  };
  return async function providerFetch(input, init = {}) {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    if (url.href !== endpoint.href)
      return reject('blocked-network', 'DSH_NETWORK', {origin: url.origin, path: url.pathname});
    if (active) return reject('limit-reached', 'DSH_REQUEST_IN_FLIGHT');
    if (seq >= limits.session_requests || !usageKnown)
      return reject('limit-reached', usageKnown ? 'DSH_REQUEST_CAP' : 'DSH_USAGE_UNKNOWN');
    let body;
    try { body = JSON.parse(init.body); } catch { return reject('runtime-fatal', 'DSH_JSON_BODY'); }
    const headers = new Headers(init.headers ?? (input instanceof Request ? input.headers : undefined));
    if (body.model !== backend.model) return reject('blocked-model', 'DSH_MODEL');
    if (!Number.isSafeInteger(body.max_tokens) || body.max_tokens < 1 || body.max_tokens > backend.max_output_tokens)
      return reject('blocked-output-limit', 'DSH_OUTPUT_LIMIT');
    if (backend.idle_check && ((headers.get('anthropic-beta') ?? '').includes('mid-conversation-tool-changes') ||
        (body.messages ?? []).some(m => Array.isArray(m.content) && m.content.some(b => b.type === 'tool_addition')))) return reject('blocked-beta', 'DSH_BETA');
    const tools = (body.tools ?? []).map(t => t.name);
    if (tools.some(n => !allow.has(n) && !n.startsWith('mcp__orrery_mail__')) ||
        !required.every(name => tools.includes(name))) return reject('blocked-tools', 'DSH_TOOLS');
    const filtered = (body.tools ?? []).filter(t => allow.has(t.name));
    audit({phase: 'assembled-request', model: body.model, tools, wire_tools: filtered.map(t => t.name)});
    if (offline) return reject('offline-capture', 'DSH_OFFLINE', {forwarded: 0});
    active = true;
    const number = ++seq, started = performance.now();
    let released = false, response, lastUsage = null, finalUsage = false;
    const finish = ok => {
      if (released) return;
      released = true; active = false; usageKnown = ok && finalUsage;
      if (response) audit({phase: 'provider-response', seq: number, status: response.status,
        usage: lastUsage, duration_ms: performance.now() - started});
      audit({phase: 'stream-end', seq: number, ok});
    };
    try {
      if (backend.idle_check) await assertIdle(fetchImpl, backend.base_url, key, audit, limits.status_ms);
      audit({phase: 'provider-request-start', seq: number});
      const timeout = AbortSignal.timeout(limits.max_request_ms);
      const signal = init.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
      response = await fetchImpl(input, {...init, headers, redirect: 'manual', signal,
        body: JSON.stringify({...body, tools: filtered})});
      if (response.status >= 300 && response.status < 400)
        return reject('blocked-redirect', 'DSH_REDIRECT', {from_origin: endpoint.origin});
      if (!response.ok) throw new Error('PROVIDER_HTTP');
      if (!response.body) { finish(true); return response; }
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let pending = '', cancelled = false;
      const observe = line => {
        if (!line.startsWith('data: ')) return;
        let event; try { event = JSON.parse(line.slice(6)); } catch { return; }
        if (event.content_block?.type === 'tool_use' && !allow.has(event.content_block.name))
          reject('blocked-model-tool', 'DSH_MODEL_TOOL');
        const usage = event.usage ?? event.message?.usage;
        if (usage) {
          lastUsage = {input: usage.input_tokens ?? lastUsage?.input ?? null,
            cache_read: usage.cache_read_input_tokens ?? lastUsage?.cache_read ?? null,
            output: usage.output_tokens ?? lastUsage?.output ?? null};
          if (event.type === 'message_delta' && Number.isFinite(usage.output_tokens)) finalUsage = true;
        }
      };
      const redact = value => key ? value.split(key).join('[REDACTED]') : value;
      const stream = new ReadableStream({
        async pull(controller) {
          try {
            const chunk = await reader.read();
            if (cancelled) return;
            pending += decoder.decode(chunk.value, {stream: !chunk.done});
            const lines = pending.split('\n'); pending = lines.pop() ?? '';
            for (const line of lines) observe(line);
            if (lines.length) controller.enqueue(new TextEncoder().encode(redact(lines.join('\n') + '\n')));
            if (chunk.done) {
              if (pending) { observe(pending); controller.enqueue(new TextEncoder().encode(redact(pending))); }
              finish(true); controller.close();
            }
          } catch { if (cancelled) return; audit({phase: 'runtime-fatal', reason: 'DSH_STREAM'}); finish(false); controller.error(new Error('DSH_STREAM')); }
        },
        async cancel(reason) { cancelled = true; try { await reader.cancel(reason); } finally { finish(false); } }
      });
      const resultHeaders = new Headers(response.headers);
      resultHeaders.delete('content-length'); resultHeaders.delete('content-encoding');
      return new Response(stream, {status: response.status, statusText: response.statusText, headers: resultHeaders});
    } catch (error) {
      const gate = [78, 80].includes(error?.dshExitCode);
      audit({phase: 'runtime-fatal', reason: gate ? 'DSH_GATE' : 'DSH_PROVIDER'}); finish(false);
      return fail(gate ? 'DSH_GATE' : 'DSH_PROVIDER', gate ? error.dshExitCode : 79);
    }
  };
}

const stateRoot = process.env.ORRERY_DSH_STATE_ROOT;
if (!stateRoot || !path.isAbsolute(stateRoot)) throw new Error('DSH_STATE_ROOT_REQUIRED');
const spec = JSON.parse(fs.readFileSync(process.env.ORRERY_DSH_BACKEND_FILE, 'utf8'));
const backendId = process.env.ORRERY_DSH_BACKEND_ID, backend = spec.backends[backendId];
if (!backend) throw new Error('DSH_BACKEND_REQUIRED');
const attemptId = process.env.ORRERY_DSH_ATTEMPT_ID;
const attempt = JSON.parse(fs.readFileSync(path.join(stateRoot, 'attempt.json'), 'utf8'));
if (!attemptId || attempt.attempt_id !== attemptId || !['spike', 'run'].includes(attempt.phase))
  throw new Error('DSH_ATTEMPT_REQUIRED');
const auditFile = process.env.ORRERY_DSH_AUDIT_FILE;
if (path.resolve(auditFile ?? '') !== path.join(stateRoot, 'audit.jsonl')) throw new Error('DSH_AUDIT_PATH');
const key = process.env.DEEPSEEK_API_KEY ?? '';
const redact = value => key ? value.split(key).join('[REDACTED]') : value;
const audit = value => fs.appendFileSync(auditFile,
  redact(JSON.stringify({...value, attempt_id: attemptId, at: new Date().toISOString()})) + '\n', {mode: 0o600});
const offline = attempt.phase === 'spike' || process.env.ORRERY_DSH_OFFLINE !== '0';
audit({phase: 'preload-ready', node: process.version, exec_path: process.execPath, offline, backend_id: backendId});
// Preserve the kit's key-before-file protection for native startup diagnostics.
const diagnostic = (target, data) => {
  if (!(typeof target === 'string' || target instanceof URL || Buffer.isBuffer(target))) return data;
  const file = path.resolve(target instanceof URL ? fileURLToPath(target) : String(target));
  if (!path.basename(file).startsWith('startup-')) return data;
  if (!file.startsWith(stateRoot + path.sep)) throw new Error('DSH_DIAGNOSTIC_SCOPE');
  return redact(typeof data === 'string' ? data : Buffer.from(data).toString('utf8'));
};
for (const [holder, name] of [[fs, 'writeFile'], [fs, 'writeFileSync'], [fsp, 'writeFile']]) {
  const original = holder[name].bind(holder);
  holder[name] = (target, data, ...rest) => original(target, diagnostic(target, data), ...rest);
}
const spawn = cp.spawn;
const proxy = path.join(os.homedir(), '.agentstack/integrations/codex_app/plugin/scripts/run-mcp.sh');
cp.spawn = function(command, args = [], options = {}) {
  if (command !== proxy) { audit({phase: 'blocked-child-process'}); throw new Error('DSH_CHILD'); }
  const source = options.env ?? process.env;
  const names = ['PATH','HOME','LANG','LC_ALL','TERM','TMPDIR','USER','LOGNAME',
    'AGENTSTACK_PROXY_AGENT_NAME','AGENTSTACK_PROXY_TOKEN_FILE','AGENTSTACK_PROXY_PROGRAM',
    'AGENTSTACK_PROJECT_KEY','AGENTSTACK_MCP_URL','AGENTSTACK_MAIL_ENV','AGENTSTACK_MAIL_HTTP_BEARER_MODE',
    'AGENTSTACK_RUNTIME_DIR','AGENTSTACK_PYTHON','AGENTSTACK_PROXY_PARENT_AGENT'];
  const env = Object.fromEntries(names.filter(n => source[n] !== undefined).map(n => [n, source[n]]));
  audit({phase: 'mcp-child-env', key_present: false, variable_names: Object.keys(env).sort()});
  return spawn.call(this, command, args, {...options, env});
};
for (const name of ['exec','execFile','fork','spawnSync','execSync','execFileSync']) cp[name] = () => {
  audit({phase: 'blocked-child-process'}); throw new Error('DSH_AUXILIARY');
};
syncBuiltinESMExports();
for (const event of ['uncaughtException','unhandledRejection']) process.prependListener(event, () => {
  audit({phase: 'runtime-fatal', reason: event}); process.exit(79);
});
globalThis.fetch = createEgress({backend, limits: spec.limits, wireTools: spec.wire_tools,
  offline, phase: attempt.phase, fetchImpl: globalThis.fetch, audit, key, fail(reason, code) {
    if (offline && reason === 'DSH_OFFLINE') process.exit(73);
    audit({phase: 'runtime-fatal', reason}); process.exit(code);
  }});
