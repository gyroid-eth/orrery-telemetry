// Normal native tools call the same ORRERY hooks as Claude/Codex.
// Mistaken credential reads or unreserved edits can leak keys or overwrite work;
// the common hooks own those decisions, without a second policy implementation.
import {execFile} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

export const name = 'orrery-tools';
export const inject = ['tools', 'workingDirectory'];

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
