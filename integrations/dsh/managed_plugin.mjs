// Ordinary path mistakes/expired reservations can touch live files. Deny in
// the registry's monotonic guard, not the TUI's fail-open DecisionEvents.
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';

export const name = 'orrery-dsh-managed';
export const inject = ['tools', 'systemPrompt', 'loader'];
const MAIL = 'mcp__orrery_mail__send_message';
const digest = p => createHash('sha256').update(fs.readFileSync(p)).digest('hex');

function contained(root, target) {
  const relative = path.relative(root, target);
  return relative !== '..' && !relative.startsWith('../') && !path.isAbsolute(relative);
}

export function canonicalTarget(root, supplied) {
  // A TUI workspace/cwd change must not retarget a checked relative path.
  if (typeof supplied !== 'string' || !path.isAbsolute(supplied) || supplied.includes('\0')) return null;
  let ancestor = path.resolve(supplied);
  const suffix = [];
  try {
    while (!fs.existsSync(ancestor)) {
      try { if (fs.lstatSync(ancestor).isSymbolicLink()) return null; }
      catch (error) { if (error.code !== 'ENOENT') return null; }
      suffix.unshift(path.basename(ancestor));
      ancestor = path.dirname(ancestor);
    }
    const target = path.join(fs.realpathSync(ancestor), ...suffix);
    if (!contained(root, target) || path.relative(root, target).split(path.sep).includes('.git')) return null;
    return target;
  } catch { return null; }
}

function matches(relative, pattern) {
  return typeof pattern === 'string' && !path.isAbsolute(pattern) &&
    !pattern.split('/').includes('..') && path.matchesGlob(relative, pattern);
}

export function createReservationGuard({attempt, task, emit, now = Date.now}) {
  const root = fs.realpathSync(task.worktree);
  const allowed = new Set(attempt.phase === 'spike' ? ['read', 'write'] : ['read', 'write', MAIL]);
  const receipts = structuredClone(task.reservation_receipts ?? []);
  const patterns = [...(task.allowed_paths ?? [])];
  let stopping = false;
  const guard = exec => {
    let reason, target, relative;
    try {
      if (stopping || !allowed.has(exec.name)) reason = 'not-in-wire-tools';
      else if (exec.name === MAIL) {
        const recipients = exec.arguments?.to;
        if (!Array.isArray(recipients) || recipients.length !== 1 || recipients[0] !== task.report_to)
          reason = 'not-in-wire-tools';
      } else {
        target = canonicalTarget(root, exec.arguments?.file_path);
        if (!target) reason = 'outside-worktree';
        else {
          relative = path.relative(root, target);
          if (!patterns.some(p => matches(relative, p))) reason = 'not-reserved';
          else {
            const matching = receipts.filter(r => Number.isSafeInteger(r.id) && r.id > 0 &&
              r.owner === attempt.agent_name && matches(relative, r.path_pattern));
            if (!matching.length) reason = 'not-reserved';
            else if (!matching.some(r => Number.isFinite(Date.parse(r.expires_ts)) && Date.parse(r.expires_ts) > now() + 1000))
              reason = 'reservation-expired';
          }
        }
      }
    } catch { reason = 'not-reserved'; }
    emit({phase: reason ? 'tool-denied' : 'tool-allowed', tool: exec.name,
      ...(relative !== undefined ? {path: relative} : {}), ...(reason ? {reason} : {})});
    return reason;
  };
  return {guard, stop: () => { stopping = true; }, root, allowed};
}

export function mailReceipt(value) {
  let receipt = value?.structuredContent;
  if (!receipt) {
    for (const block of value?.content ?? []) {
      if (block.type !== 'text') continue;
      try { receipt = JSON.parse(block.text); break; } catch { /* not JSON */ }
    }
  }
  const delivery = receipt?.deliveries?.[0]?.payload;
  if (!delivery) return null;
  return {message_id: delivery.id, to: Array.isArray(delivery.to) && delivery.to.length === 1 ? delivery.to[0] : delivery.to, from: delivery.from,
    verified_sender: receipt.verified_sender === true,
    ok: receipt.verified_sender === true && Number.isSafeInteger(delivery.id)};
}

export function installSessionEvents(ctx, emit, currentSession) {
  return ctx.on('session/event', (session, event) => {
    if (session.id !== currentSession()) return;
    if (event.type === 'turn/start') emit({phase: 'task-start', turn: event.data.turn});
    if (event.type === 'turn/end') emit({phase: 'turn-end', reason_kind: event.data.reason.kind, turn: event.data.turn});
  });
}

export function apply(ctx) {
  const state = process.env.ORRERY_DSH_STATE_ROOT;
  if (!state || !path.isAbsolute(state)) throw new Error('DSH_STATE_ROOT_REQUIRED');
  const attempt = JSON.parse(fs.readFileSync(path.join(state, 'attempt.json'), 'utf8'));
  const task = JSON.parse(fs.readFileSync(path.join(state, 'task.json'), 'utf8'));
  if (!['spike', 'run'].includes(attempt.phase) || !attempt.agent_name ||
      attempt.attempt_id !== task.attempt_id || attempt.attempt_id !== process.env.ORRERY_DSH_ATTEMPT_ID)
    throw new Error('DSH_MANIFEST_MISMATCH');
  if (attempt.phase === 'run' && task.reservation_source === 'offline-fixture')
    throw new Error('DSH_FIXTURE_RECEIPT_IN_RUN');
  const emit = event => fs.appendFileSync(path.join(state, 'events.jsonl'), JSON.stringify({
    ...event, attempt_id: attempt.attempt_id, task_id: task.task_id, at: new Date().toISOString(),
  }) + '\n');
  const policy = createReservationGuard({attempt, task, emit});
  ctx.tools.guard(policy.guard);
  ctx.systemPrompt.section({name: 'orrery:worktree', order: 500,
    text: 'Use absolute file paths. This task may read and write only inside ' + policy.root + '.'});
  const verify = scope => {
    const adapter = [...ctx.get('loader').entries()].find(e => e.options.id === 'llm-deepseek');
    if (adapter?.options.config?.retryPolicy?.maxRetries !== 0) {
      policy.stop();
      throw new Error('DSH_RETRY_MUST_BE_ZERO');
    }
    const names = ctx.tools.schemas(scope).map(t => t.name).sort();
    if (JSON.stringify(names) !== JSON.stringify([...policy.allowed].sort())) {
      policy.stop();
      emit({phase: 'tool-set-mismatch', tools: names});
      throw new Error('DSH_TOOL_SET_MISMATCH');
    }
    emit({phase: 'plugin-ready', tools: names, retry_count: 0});
  };
  let sessionId;
  ctx.on('agent/created', async ({agent}) => {
    sessionId ??= agent.session.id;
    agent.ctx.tools.restrict({allow: [...policy.allowed]});
    verify(agent);
    if (attempt.phase === 'spike' && task.spike_probe === true) {
      const call = (id, file_path) => agent.ctx.tools.execute({
        callId: 'spike-' + id, name: 'write', agent,
        arguments: {file_path: path.resolve(policy.root, file_path), content: 'offline guard probe'},
        signal: new AbortController().signal,
      });
      const target = path.join(policy.root, 'probe/ok.txt');
      if (fs.existsSync(target)) await agent.ctx.tools.execute({
        callId: 'spike-read-before-write', name: 'read', agent,
        arguments: {file_path: target}, signal: new AbortController().signal,
      });
      const positive = await call('positive', 'probe/ok.txt');
      const denied = [];
      for (const [id, file] of [['outside', '../outside.txt'], ['missing', 'probe/unreserved.txt'], ['expired', 'probe/expired.txt']])
        denied.push((await call(id, file)).isError === true);
      const passed = positive.isError === false && denied.every(Boolean) &&
        fs.readFileSync(path.join(policy.root, 'probe/ok.txt'), 'utf8') === 'offline guard probe';
      emit({phase: 'spike-guard-probe', passed, positive_error: positive.isError,
        ...(positive.isError ? {positive_reason: positive.error.info?.code ?? positive.error.message} : {}), denied});
      if (!passed) { policy.stop(); throw new Error('DSH_SPIKE_GUARD_PROBE_FAILED'); }
    }
  });
  ctx.on('agent/pre-step', async ({agent}, next) => {
    verify(agent);
    return next();
  });
  ctx.on('tools/result', (exec, result) => {
    if (result.isError) return;
    if (exec.name === 'write') {
      const target = canonicalTarget(policy.root, exec.arguments?.file_path);
      if (target && fs.statSync(target).isFile())
        emit({phase: 'artifact-written', path: path.relative(policy.root, target), sha256: digest(target)});
    } else if (exec.name === MAIL) {
      const receipt = mailReceipt(result.value);
      if (receipt) emit({phase: 'mail-sent', ...receipt});
    }
  });
  installSessionEvents(ctx, emit, () => sessionId);
  ctx.on('dispose', () => policy.stop());
}
