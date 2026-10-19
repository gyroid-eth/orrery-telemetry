// Official SDK client for the unchanged product stdio proxy. No Mail HTTP fallback.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';

export function unwrap(reply) {
  if (reply?.isError) throw new Error('MAIL_RPC_FAILED');
  return reply.structuredContent ?? JSON.parse(reply.content.find(c => c.type === 'text').text);
}

export function checkBinding(status, who, identity, proxy) {
  if (status.agent_name !== identity.agent_name || status.project_key !== identity.project_key ||
      status.program !== proxy.env.AGENTSTACK_PROXY_PROGRAM || status.lineage?.parent_agent !== identity.parent)
    throw new Error('MAIL_BINDING_MISMATCH');
  if (who.name !== identity.agent_name || who.program !== identity.program || who.retired_at)
    throw new Error('MAIL_REMOTE_IDENTITY_MISMATCH');
}

export function normalizeReservations(result, paths, owner, now = Date.now()) {
  const granted = result.granted ?? [];
  if (result.conflicts?.length || granted.length !== paths.length ||
      new Set(granted.map(r => r.path_pattern)).size !== paths.length)
    throw new Error('MAIL_RESERVATION_CONFLICT_OR_PARTIAL');
  return granted.map(row => {
    if (!Number.isSafeInteger(row.id) || row.id <= 0 || !paths.includes(row.path_pattern) ||
        (row.owner !== undefined && row.owner !== owner) ||
        !(Date.parse(row.expires_ts) > now + 600_000 - 5000))
      throw new Error('MAIL_RESERVATION_INVALID');
    return {id: row.id, path_pattern: row.path_pattern, expires_ts: row.expires_ts, owner};
  });
}

export function releaseReceipt({attempt, ids, result}) {
  const confirmed = ids.length > 0 && result.released === ids.length;
  return {attempt_id: attempt.attempt_id, agent_name: attempt.agent_name,
    reservation_ids: ids, released_ids: confirmed ? ids : [], confirmed,
    source: 'product-bound-proxy', checked_at: new Date().toISOString()};
}

function write(file, value) {
  fs.writeFileSync(file, JSON.stringify(value, null, 2) + '\n', {mode: 0o600});
}

export async function main() {
  const [state, action = 'probe', fixture = ''] = process.argv.slice(2);
  if (!state || !path.isAbsolute(state) || !['probe', 'reserve', 'release'].includes(action))
    throw new Error('MAIL_PROBE_ARGUMENTS');
  const read = name => JSON.parse(fs.readFileSync(path.join(state, name), 'utf8'));
  const config = read('run-config.json'), identity = read('mail-identity.json');
  const original = read('mcp.json').mcpServers['orrery-mail'];
  const expectedProxy = path.join(config.product_root, 'integrations/codex_app/plugin/scripts/run-mcp.sh');
  if (original.command !== expectedProxy || JSON.stringify(original.args) !== '[]' ||
      original.env.AGENTSTACK_PROXY_AGENT_NAME !== identity.agent_name ||
      original.env.AGENTSTACK_PROJECT_KEY !== identity.project_key)
    throw new Error('MAIL_GENERATED_CONFIG_MISMATCH');
  if (!['', 'backend-unreachable', 'process-unreachable'].includes(fixture)) throw new Error('MAIL_FIXTURE_ARGUMENT');
  const proxy = {...original, env: {...original.env}};
  if (fixture === 'backend-unreachable') proxy.env.AGENTSTACK_MCP_URL = 'http://127.0.0.1:1/mcp';
  if (fixture === 'process-unreachable') proxy.command = '/usr/bin/false';
  const req = createRequire(path.join(config.runtime, 'node_modules/@deepseek-ai/dsh-mcp-client/package.json'));
  const {Client} = await import(pathToFileURL(req.resolve('@modelcontextprotocol/client')));
  const {StdioClientTransport} = await import(pathToFileURL(req.resolve('@modelcontextprotocol/client/stdio')));
  const client = new Client({name: 'orrery-dsh-preflight', version: '1'},
    {capabilities: {}, versionNegotiation: {mode: 'auto'}});
  // Explicit allowlist: API keys and owner-token values are never inherited.
  const env = {...proxy.env, HOME: config.home,
    PATH: path.dirname(config.node) + ':/usr/bin:/bin', LANG: 'en_US.UTF-8', LC_ALL: 'en_US.UTF-8'};
  const transport = new StdioClientTransport({command: proxy.command, args: proxy.args,
    env, cwd: config.worktree, stderr: 'pipe'});
  const report = {schema: false, backend: false, ok: false, fixture,
    agent_name: identity.agent_name, at: new Date().toISOString()};
  const reportName = fixture ? `mail-probe-${fixture}.json` : 'mail-probe.json';
  let timeout;
  try {
    await Promise.race([(async () => {
      await client.connect(transport);
      const listing = await client.listTools();
      report.tools = listing.tools.map(t => t.name);
      report.schema = ['runtime_status', 'whois', 'send_message', 'reserve_files', 'release_reservations']
        .every(name => report.tools.includes(name));
      if (!report.schema || client.getServerCapabilities()?.resources) throw new Error('MAIL_SCHEMA');
      const status = unwrap(await client.callTool({name: 'runtime_status', arguments: {}}));
      const who = unwrap(await client.callTool({name: 'whois', arguments: {name: identity.agent_name}}));
      checkBinding(status, who, identity, original);
      report.backend = true;
      if (fixture) throw new Error('NEGATIVE_FIXTURE_UNEXPECTEDLY_REACHED_BACKEND');
      if (action === 'reserve') {
        const task = read('task.json');
        // Never repeat a potentially successful partial reservation implicitly.
        const intent = path.join(state, 'reservation-intent.json');
        fs.writeFileSync(intent, JSON.stringify({paths: task.allowed_paths, at: report.at}), {flag: 'wx', mode: 0o600});
        const response = unwrap(await client.callTool({name: 'reserve_files', arguments: {
          paths: task.allowed_paths, exclusive: true, ttl_seconds: 1800, reason: 'Managed dsh task'}}));
        write(path.join(state, 'reservation-result.json'), response);
        // A conflict may still grant a subset. Keep those real IDs for release,
        // while complete=false prevents using them to start the model.
        const partial = (response.granted ?? []).filter(row => Number.isSafeInteger(row.id) && row.id > 0 &&
          task.allowed_paths.includes(row.path_pattern)).map(row => ({id: row.id,
            path_pattern: row.path_pattern, expires_ts: row.expires_ts, owner: identity.agent_name}));
        write(path.join(state, 'reservations.json'), {agent_name: identity.agent_name,
          attempt_id: task.attempt_id, receipts: partial, complete: false});
        const receipts = normalizeReservations(response, task.allowed_paths, identity.agent_name);
        write(path.join(state, 'reservations.json'), {agent_name: identity.agent_name,
          attempt_id: task.attempt_id, receipts, complete: true});
      } else if (action === 'release') {
        const attempt = read('attempt.json');
        const exit = read('runtime-exit.json');
        if (exit.group_absent !== true || exit.group_check !== 'observed') throw new Error('MAIL_GROUP_STILL_UNKNOWN');
        const reservation = read('reservations.json');
        if (reservation.attempt_id !== attempt.attempt_id || reservation.agent_name !== identity.agent_name ||
            attempt.agent_name !== identity.agent_name) throw new Error('MAIL_RELEASE_IDENTITY');
        const ids = reservation.receipts.map(r => r.id);
        const result = unwrap(await client.callTool({name: 'release_reservations', arguments: {file_reservation_ids: ids}}));
        const receipt = releaseReceipt({attempt, ids, result});
        write(path.join(state, 'release-receipt.json'), receipt);
        if (!receipt.confirmed) throw new Error('MAIL_RELEASE_UNCONFIRMED');
      }
      report.ok = true;
    })(), new Promise((_, reject) => {timeout = setTimeout(() => reject(new Error('MAIL_TIMEOUT')), 20000);})]);
  } catch (error) {
    // Do not persist arbitrary backend errors, stderr, instructions or env values.
    report.failure = 'MAIL_STEP_FAILED';
  } finally {
    clearTimeout(timeout);
    await client.close().catch(() => {});
    write(path.join(state, reportName), report);
  }
  return report.ok ? 0 : 2;
}

if (import.meta.main) {
  try {process.exitCode = await main();}
  catch {process.stderr.write('Mail step failed; preserve state\n'); process.exitCode = 2;}
}
