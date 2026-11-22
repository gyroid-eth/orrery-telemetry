// Normal native tools call the same ORRERY hooks as Claude/Codex.
// Mistaken credential reads or unreserved edits can leak keys or overwrite work;
// the common hooks own those decisions, without a second policy implementation.
import {execFile} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

export const name = 'orrery-tools';
export const inject = ['tools', 'workingDirectory'];

// All callbacks for one native CLI launch share this latch. It prevents a
// contender's failed marker write from being forgotten by the lock owner.
// The owner leaves its on-disk lock behind, so failure survives process exit.
const failedHistoryLaunches = new Set();

export async function saveHistoryBinding(agent, config, persistence) {
  const env = {...process.env, ...config.env};
  const launch = config.history_binding;
  if (!launch || env.AGENTSTACK_RUNTIME_MODE === 'fresh' || env.AGENTSTACK_RUNTIME_MODE === 'global'
      || 'AGENTSTACK_FRESH_CLIENT_CONTEXT' in env || agent?.session?.header?.parentSession) return;
  const id = agent?.session?.id;
  if (typeof id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$/.test(id)
      || agent.session.header?.id !== id) return;
  const receipt = path.join(config.state_dir, 'dsh-history.json');
  const lock = path.join(config.state_dir, 'dsh-history-' + launch.launch_id + '.lock');
  const conflict = path.join(config.state_dir, 'dsh-history-' + launch.launch_id + '.conflict');
  const observed = path.join(config.state_dir, 'dsh-history-' + launch.launch_id + '.root.json');
  const index = path.join(path.dirname(path.dirname(path.dirname(config.state_dir))), 'dsh-history', launch.agent_name + '.json');
  const currentLaunch = () => JSON.stringify(JSON.parse(fs.readFileSync(index, 'utf8'))) === JSON.stringify(launch);
  let temporary;
  let locked = false;
  let observationRecorded = false;
  const markConflict = () => {
    // Publish failure in memory before attempting I/O: the callback that owns
    // the lock may be awaiting flush and must also stop publication/cleanup.
    failedHistoryLaunches.add(lock);
    fs.writeFileSync(conflict, '', {mode: 0o600});
  };
  try {
    if (failedHistoryLaunches.has(lock) || !currentLaunch()) return; // Old nonces cannot poison new ones.
    try {
      fs.closeSync(fs.openSync(lock, 'wx', 0o600));
      locked = true;
    } catch (error) {
      if (error.code === 'EEXIST') markConflict();
      return;
    }
    if (!currentLaunch() || fs.existsSync(conflict)) return;
    let previous, first;
    try { previous = JSON.parse(fs.readFileSync(receipt, 'utf8')); }
    catch (error) { if (error.code !== 'ENOENT') {markConflict();return;} }
    if (previous?.launch_id === launch.launch_id && (previous.conflicted || previous.session_id !== id)) {
      markConflict();return;
    }
    try { first = JSON.parse(fs.readFileSync(observed, 'utf8')); }
    catch (error) { if (error.code !== 'ENOENT') {markConflict();return;} }
    if (first && (first.launch_id !== launch.launch_id || first.session_id !== id)) {
      markConflict();return;
    }
    if (!first) fs.writeFileSync(observed, JSON.stringify({launch_id:launch.launch_id,session_id:id})+'\n',
      {mode:0o600,flag:'wx'});
    observationRecorded = true;
    // Once issued, keep the descriptor identity and timestamp fixed. Retrying
    // must never rebind a replaced artifact. Different roots still conflict
    // above, even when the original root already has its receipt.
    if (previous?.launch_id === launch.launch_id) return;
    // Root identity is now recorded independently of locator/flush/receipt I/O.
    // locate() is the native JSONL backend's exact address, not a cwd scan.
    if (typeof persistence?.locate !== 'function' || typeof persistence?.flush !== 'function') return;
    const location = persistence.locate(agent.session.header);
    if (location?.kind !== 'jsonl' || !path.isAbsolute(location.path)) return;
    await persistence.flush(); // The native owner may still defer publication until activity.
    const info = fs.lstatSync(location.path);
    if (!info.isFile() || info.size === 0) return;
    if (!currentLaunch() || failedHistoryLaunches.has(lock) || fs.existsSync(conflict)) return;
    const claim = {...launch, session_id: id, transcript_path: location.path,
      device: info.dev, inode: info.ino, conflicted: false,
      created_at: new Date().toISOString()};
    temporary = receipt + '.' + process.pid + '.tmp';
    fs.writeFileSync(temporary, JSON.stringify(claim) + '\n', {mode: 0o600});
    fs.renameSync(temporary, receipt);
  } catch {
    // Optional legacy display proof cannot prevent native execution.
  } finally {
    if (temporary) { try { fs.unlinkSync(temporary); } catch { /* Renamed or absent. */ } }
    // An unrecorded observation cannot be treated as no observation. In both
    // failure cases retain the durable lock until a new formal launch nonce.
    if (locked && !observationRecorded) failedHistoryLaunches.add(lock);
    if (locked && observationRecorded && !failedHistoryLaunches.has(lock)) {
      try { fs.unlinkSync(lock); } catch { /* Remain unbound if cleanup fails. */ }
    }
  }
}

export function payloadFor(exec, config) {
  const names = {bash: 'Bash', pwsh: 'Bash', read: 'Read', read_file: 'Read',
    write: 'Write', write_file: 'Write', edit: 'Edit'};
  return {session_id: exec.agent?.session?.id ?? '',
    cwd: exec.agent?.options?.cwd ?? config.cwd,
    tool_name: names[exec.name] ?? exec.name, tool_input: exec.arguments ?? {}};
}

export function callHook(file, payload, env = process.env) {
  return new Promise(resolve => {
    const child = execFile(file, [], {env, cwd: payload.cwd, maxBuffer: 1024 * 1024}, (error, stdout, stderr) => {
      // A normal warn-open outage must remain visible, without exposing the
      // hook's private diagnostics. Only its public success schema is shown.
      if (!error) {
        let warning;
        try { warning = JSON.parse(stdout); } catch { /* Non-JSON output is private. */ }
        if (warning && !Array.isArray(warning) && typeof warning === 'object' &&
            typeof warning.systemMessage === 'string' && warning.systemMessage)
          process.stderr.write(warning.systemMessage + '\n');
      }
      let reason = 'ORRERY hook denied: ' + path.basename(file);
      if (error?.code === 2 && !error.killed && !error.signal) {
        // Match Hermes thin_guard._denial_reason and Claude's public schema.
        // execFile decodes UTF-8 with replacement, as the Hermes adapter does.
        let publicOutput;
        try { publicOutput = JSON.parse(stdout); } catch { /* Arbitrary stdout is private. */ }
        let detail = publicOutput && !Array.isArray(publicOutput) ? publicOutput.reason : undefined;
        if (typeof detail !== 'string' || !detail.trim())
          detail = publicOutput?.hookSpecificOutput?.permissionDecisionReason;
        if (typeof detail !== 'string' || !detail.trim()) detail = stderr || '';
        detail = detail.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '');
        if (!/^\s*(?:Traceback \(|File ".*", line \d+|\w*(?:Error|Exception):)|: line \d+:/m.test(detail)) {
          detail = detail.trim();
          detail = Array.from(detail).filter(c => !/[\p{C}\p{Z}]/u.test(c) || ' \n\t'.includes(c)).slice(0, 2000).join('');
          if (detail) reason += '\n' + detail;
        }
      }
      resolve(error ? {ok: false, reason} : {ok: true});
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

export async function saveSession(agent, config, runHook = callHook) {
  const id = agent?.session?.id;
  if (typeof id !== 'string' || !id || agent.session.header?.parentSession) return;
  try {
    fs.writeFileSync(path.join(config.state_dir, 'native-session.json'), JSON.stringify({
      harness: 'dsh', native_session_id: id, created_at: new Date().toISOString(),
    }) + '\n', {mode: 0o600});
  } catch (error) {
    if (process.env.AGENTSTACK_RUNTIME_MODE === 'fresh' || 'AGENTSTACK_FRESH_CLIENT_CONTEXT' in process.env)
      throw new Error('ORRERY fresh native session could not be saved');
    return; // Legacy native resume metadata remains optional.
  }
  const env = {...process.env, ...config.env};
  if (env.AGENTSTACK_RUNTIME_MODE === 'fresh' || 'AGENTSTACK_FRESH_CLIENT_CONTEXT' in env) {
    const decision = await runHook(path.join(config.hooks_dir, 'check-agent-registered.sh'),
      {hook_event_name: 'SessionStart', session_id: id, cwd: config.cwd}, env);
    if (!decision.ok) throw new Error(decision.reason);
  }
}

export function apply(ctx, config, runHook = callHook) {
  const env = {...process.env, ...config.env};
  const hook = name => path.join(config.hooks_dir, name);
  ctx.on('agent/created', ({agent}) => saveSession(agent, config, runHook));
  // Keep the original tools/guard dependency and all fresh callbacks unchanged.
  if (config.history_binding && env.AGENTSTACK_RUNTIME_MODE !== 'fresh'
      && env.AGENTSTACK_RUNTIME_MODE !== 'global' && !('AGENTSTACK_FRESH_CLIENT_CONTEXT' in env)) {
    // Native creation listeners are serial. Observe every root even while the
    // optional persistence service is absent; its injected listener publishes
    // only after the same observation and a successful materialization.
    // The TUI defers its writer's first flush until real activity. Creation
    // can therefore observe the root before there is any physical artifact.
    // Check again at durability checkpoints and turn completion. Include the
    // independent observer in this queue: the projection cache checkpoints on
    // session/created before agent/created, without awaiting the first flush.
    // Different Sessions retain the normal launch-lock conflict behavior.
    const pending = new WeakMap();
    const retry = (session, persistence) => {
      const next = (pending.get(session) ?? Promise.resolve()).then(() =>
        saveHistoryBinding({session}, config, persistence));
      pending.set(session, next);
      return next;
    };
    ctx.on('agent/created', ({agent}) => retry(agent.session));
    ctx.inject(['sessionPersistence'], inner => {
      inner.on('agent/created', ({agent}) => retry(agent.session, inner.sessionPersistence));
      inner.on('session/flush', session => retry(session, inner.sessionPersistence));
      inner.on('session/event', (session, event) => {
        if (event.type === 'turn/end') return retry(session, inner.sessionPersistence);
      });
    });
  }
  ctx.on('tools/pre-execute', async (exec, next) => {
    const payload = payloadFor(exec, config);
    if (exec.agent) payload.cwd = await ctx.workingDirectory.ensure(exec.agent, exec.signal);
    if (payload.tool_name === 'Bash' && typeof payload.tool_input.workdir === 'string')
      payload.cwd = path.resolve(payload.cwd, payload.tool_input.workdir);
    const hooks = ['deny-llm-key-read.py'];
    if (['Bash', 'Write', 'Edit'].includes(payload.tool_name)) hooks.push('check-agent-registered.sh');
    if (['Write', 'Edit'].includes(payload.tool_name)) hooks.push('check-file-reservation.sh');
    for (const file of hooks) {
      const result = await runHook(hook(file), payload, env);
      if (!result.ok) return {kind: 'deny', reason: result.reason};
    }
    return next();
  });
  ctx.on('tools/result', async (exec, result) => {
    const payload = payloadFor(exec, config);
    if (exec.agent) payload.cwd = await ctx.workingDirectory.ensure(exec.agent, exec.signal);
    if (!result.isError && ['Write', 'Edit'].includes(payload.tool_name)) {
      // No tool content is needed by the release hook; only success and path.
      await runHook(hook('release-file-reservation.sh'), {...payload, tool_result: {success: true}}, env);
    }
  });
}
