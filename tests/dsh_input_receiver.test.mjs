import test from 'node:test';
import assert from 'node:assert/strict';
import {MailReceiver, sameInputTarget, pasteOutcome} from '../integrations/dsh/input_receiver.mjs';

const binding = {identity: 'fixture-agent', ownerGeneration: 'owner-one', sessionId: 'session-one'};
const message = id => ({...binding, messageId: id, text: `task-${id}`});
function fixture() {
  let saved = null;
  const calls = [];
  const journal = {read: async () => structuredClone(saved),
    write: async state => {saved = structuredClone(state);}};
  const dependencies = {binding, journal,
    admit: async entry => ({allowed: true, turnId: `turn-${entry.messageId}`, budgetGeneration: 'budget-one'}),
    submit: async entry => {
      // native呼出しの前にintentが保存済みであることを消費側から確認する。
      assert.equal(saved.entries.find(item => item.messageId === entry.messageId).phase, 'submit-intent');
      calls.push(entry.messageId);
      return {accepted: true, deliveryKey: entry.deliveryKey, turnId: entry.permit.turnId};
    }};
  return {dependencies, calls, snapshot: () => structuredClone(saved),
    receiver: () => new MailReceiver(dependencies)};
}

function deferred() {
  let resolve;
  const promise = new Promise(done => {resolve = done;});
  return {promise, resolve};
}

test('受領ACKはdurable保存後、同IDの並行通知は一度だけ受領', async () => {
  const f = fixture(), receiver = f.receiver();
  const receipts = await Promise.all([receiver.receive(message(1)), receiver.receive(message(1))]);
  assert.deepEqual(receipts.map(item => item.duplicate), [false, true]);
  assert.equal(f.snapshot().entries.length, 1);
  assert.deepEqual(f.calls, []);
});

test('intentのdurable完了前はsubmit 0、保存解放後に1件だけ進む', async () => {
  const f = fixture(), entered = deferred(), release = deferred(), attempts = [];
  const submit = f.dependencies.submit;
  f.dependencies.submit = async entry => {
    // saved.phaseのassertより前に呼出し自体を数え、早すぎるsubmitも検出する。
    attempts.push(entry.messageId);
    return submit(entry);
  };
  const receiver = f.receiver();
  await receiver.receive(message(1));
  const write = f.dependencies.journal.write;
  f.dependencies.journal.write = async state => {
    if (state.entries.some(entry => entry.phase === 'submit-intent')) {
      entered.resolve();
      await release.promise;
    }
    await write(state);
  };
  const draining = receiver.drain();
  try {
    await entered.promise;
    await new Promise(setImmediate);
    assert.deepEqual(attempts, []);
    assert.equal(f.snapshot().entries[0].phase, 'queued');
  } finally {
    release.resolve();
  }
  assert.equal((await draining).kind, 'submitted');
  assert.deepEqual(attempts, [1]);
});

test('保存失敗は受領成功にせず、別宛先も配送しない', async () => {
  const f = fixture();
  f.dependencies.journal.write = async () => {throw new Error('fixture保存不可');};
  await assert.rejects(f.receiver().receive(message(1)));
  await assert.rejects(f.receiver().receive({...message(1), ownerGeneration: 'other'}));
  assert.deepEqual(f.calls, []);
});

test('busyではFIFO保留、idleごとに一件だけsubmit、再通知で二重実行しない', async () => {
  const f = fixture(), receiver = f.receiver();
  await receiver.setBusy(true);
  await receiver.receive(message(2)); await receiver.receive(message(1));
  assert.equal((await receiver.drain()).kind, 'held');
  await receiver.setBusy(false);
  assert.equal((await receiver.drain()).kind, 'submitted');
  assert.equal((await receiver.drain()).kind, 'held');
  await receiver.receive(message(2));
  await receiver.setBusy(false); await receiver.drain();
  assert.deepEqual(f.calls, [2, 1]);
});

test('予算/guard拒否・例外ではsubmitしない', async () => {
  const f = fixture(), receiver = f.receiver();
  await receiver.receive(message(1));
  f.dependencies.admit = async () => ({allowed: false});
  assert.equal((await f.receiver().drain()).reason, 'not-admitted');
  f.dependencies.admit = async () => {throw new Error('fixture unavailable');};
  assert.equal((await f.receiver().drain()).reason, 'admission-unavailable');
  assert.deepEqual(f.calls, []);
});

test('submit後ACK喪失はunknown、再起動・再通知後も自動再submitしない', async () => {
  const f = fixture();
  f.dependencies.submit = async entry => {f.calls.push(entry.messageId); throw new Error('ACK喪失');};
  let receiver = f.receiver();
  await receiver.receive(message(1));
  assert.equal((await receiver.drain()).kind, 'unknown');
  receiver = f.receiver(); await receiver.recover(); await receiver.setBusy(false);
  await receiver.receive(message(1)); await receiver.receive(message(2));
  assert.equal((await receiver.drain()).reason, 'submit-unknown');
  assert.deepEqual(f.calls, [1]);
});

test('submit成功後のreceipt保存失敗はintentを残し、再起動後unknownとして止める', async () => {
  const f = fixture(), receiver = f.receiver();
  const write = f.dependencies.journal.write;
  f.dependencies.journal.write = async state => {
    if (state.entries.some(entry => entry.phase === 'submitted')) throw new Error('receipt保存中断');
    await write(state);
  };
  await receiver.receive(message(1)); await assert.rejects(receiver.drain());
  assert.equal(f.snapshot().entries[0].phase, 'submit-intent');
  f.dependencies.journal.write = write;
  const restarted = f.receiver(); await restarted.recover(); await restarted.setBusy(false);
  assert.equal((await restarted.drain()).reason, 'submit-unknown');
  assert.deepEqual(f.calls, [1]);
});

test('停止中の通知を受領成功にせず、既存queueもsubmitしない', async () => {
  const f = fixture(), receiver = f.receiver();
  await receiver.receive(message(1)); await receiver.stop();
  assert.equal((await receiver.receive(message(2))).accepted, false);
  assert.equal((await receiver.drain()).kind, 'held');
  assert.deepEqual(f.calls, []);
});

const target = {socket: 'default', session: 'fixture-agent', pane: '%1',
  ownerGeneration: 'owner-one', runtimePid: 100, inputEpoch: 'epoch-one', ready: true};
test('通常pasteは同じ対象へ残りとCRを送り、対象変更時だけ停止する', () => {
  const continuePaste = actual => {
    const effects = ['paste-prefix'];
    if (sameInputTarget(target, actual)) effects.push('paste-rest', 'CR');
    return effects;
  };
  assert.equal(sameInputTarget(target, {...target}), true);
  assert.deepEqual(continuePaste({...target}), ['paste-prefix', 'paste-rest', 'CR']);
  for (const changed of [{socket: 'other'}, {session: 'other'}, {pane: '%2'},
    {ownerGeneration: 'owner-two'}, {runtimePid: 101}, {inputEpoch: 'epoch-two'}, {ready: false}]) {
    const outcome = pasteOutcome({totalBytes: 12, acknowledgedBytes: 4,
      transportKnown: true, submitState: 'not-sent'});
    assert.deepEqual(continuePaste({...target, ...changed}), ['paste-prefix']);
    assert.equal(outcome.phase, 'partial');
    assert.equal(outcome.automaticResend, false);
    assert.equal(outcome.maySendSubmit, false);
  }
});

test('既知partialと到達量/submit不明を区別し、いずれも二重入力しない', () => {
  const base = {totalBytes: 12, acknowledgedBytes: 12, transportKnown: true, submitState: 'not-sent'};
  assert.equal(pasteOutcome(base).phase, 'partial');
  assert.equal(pasteOutcome({...base, acknowledgedBytes: 0}).phase, 'not-sent');
  assert.equal(pasteOutcome({...base, transportKnown: false}).phase, 'unknown');
  assert.equal(pasteOutcome({...base, submitState: 'unknown'}).phase, 'unknown');
  assert.equal(pasteOutcome({...base, submitState: 'acknowledged'}).phase, 'submitted');
  for (const input of [base, {...base, transportKnown: false}, {...base, submitState: 'unknown'}]) {
    assert.equal(pasteOutcome(input).automaticResend, false);
    assert.equal(pasteOutcome(input).maySendSubmit, false);
  }
});
