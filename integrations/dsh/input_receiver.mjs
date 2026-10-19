// 中断・ACK喪失・busyは通常起こり得、二重入力/重複要求の害がある。
// trusted handoff後のplugin core。owner認証/IPC/永続storeは共通側へ委譲する。
export class MailReceiver {
  constructor({binding, journal, admit, submit}) {
    this.binding = {...binding};
    this.journal = journal;
    this.admit = admit;
    this.submit = submit;
    this.tail = Promise.resolve();
  }

  exclusive(action) {
    const result = this.tail.then(action);
    this.tail = result.catch(() => {});
    return result;
  }

  async read() {
    return structuredClone(await this.journal.read() ?? {
      nextSeq: 1, busy: false, stopping: false, entries: [],
    });
  }

  receive(message) {
    return this.exclusive(async () => {
      for (const key of ['identity', 'ownerGeneration', 'sessionId']) {
        if (message[key] !== this.binding[key]) throw new Error('通知の宛先が一致しません');
      }
      if (!Number.isSafeInteger(message.messageId) || message.messageId <= 0 ||
          typeof message.text !== 'string') throw new Error('通知の形式が不正です');
      const state = await this.read();
      if (state.stopping) return {accepted: false, reason: 'stopping'};
      const previous = state.entries.find(entry => entry.messageId === message.messageId);
      if (previous) return this.receipt(previous, true);
      const entry = {messageId: message.messageId, text: message.text,
        deliveryKey: `${this.binding.ownerGeneration}:${message.messageId}`,
        seq: state.nextSeq++, phase: 'queued'};
      state.entries.push(entry);
      // writeはdurable完了まで返らない契約。成功前にwatcher ACKを返さない。
      await this.journal.write(state);
      return this.receipt(entry, false);
    });
  }

  receipt(entry, duplicate) {
    return {accepted: true, messageId: entry.messageId, deliveryKey: entry.deliveryKey,
      seq: entry.seq, duplicate};
  }

  setBusy(busy) {
    return this.exclusive(async () => {
      const state = await this.read();
      state.busy = busy;
      await this.journal.write(state);
    });
  }

  stop() {
    return this.exclusive(async () => {
      const state = await this.read();
      state.stopping = true;
      await this.journal.write(state);
    });
  }

  recover() {
    return this.exclusive(async () => {
      const state = await this.read();
      for (const entry of state.entries) {
        if (entry.phase === 'submit-intent') entry.phase = 'unknown';
      }
      await this.journal.write(state);
      // busy解除や再submitはしない。native receiptとの照合は呼出し元の責任。
    });
  }

  drain() {
    return this.exclusive(async () => {
      const state = await this.read();
      if (state.stopping || state.busy) return {kind: 'held', reason: 'not-idle'};
      const entry = state.entries.find(item => item.phase !== 'submitted');
      if (!entry) return {kind: 'empty'};
      if (entry.phase !== 'queued') return {kind: 'held', reason: 'submit-unknown'};
      let permit;
      try {
        permit = await this.admit(structuredClone(entry));
      } catch {
        return {kind: 'held', reason: 'admission-unavailable'};
      }
      if (permit?.allowed !== true || typeof permit.turnId !== 'string' || !permit.turnId) {
        return {kind: 'held', reason: 'not-admitted'};
      }
      // 共通admissionのprivate owner/token値をjournalへ保存しない。
      entry.permit = {turnId: permit.turnId, budgetGeneration: permit.budgetGeneration};
      entry.phase = 'submit-intent';
      state.busy = true;
      await this.journal.write(state);
      let receipt;
      try {
        receipt = await this.submit(structuredClone(entry));
      } catch {
        receipt = null;
      }
      const confirmed = receipt?.accepted === true &&
        receipt.deliveryKey === entry.deliveryKey && receipt.turnId === entry.permit.turnId;
      entry.phase = confirmed ? 'submitted' : 'unknown';
      if (confirmed) entry.nativeReceipt = {
        accepted: true, deliveryKey: receipt.deliveryKey, turnId: receipt.turnId,
      };
      await this.journal.write(state);
      return {kind: entry.phase, deliveryKey: entry.deliveryKey};
    });
  }
}

// owned targetの検証結果を使う。これはowner認証/生process検査そのものではない。
export function sameInputTarget(expected, actual) {
  return actual?.ready === true && ['socket', 'session', 'pane', 'ownerGeneration',
    'runtimePid', 'inputEpoch'].every(key => expected[key] !== undefined && expected[key] === actual[key]);
}

// 回復時の分類。送信を試みたbytes数を「到達確認済み」に読み替えない。
export function pasteOutcome({totalBytes, acknowledgedBytes, transportKnown, submitState}) {
  let phase;
  if (!transportKnown || submitState === 'unknown' ||
      !Number.isSafeInteger(totalBytes) || totalBytes <= 0 ||
      !Number.isSafeInteger(acknowledgedBytes) || acknowledgedBytes < 0 || acknowledgedBytes > totalBytes) {
    phase = 'unknown';
  } else if (submitState === 'acknowledged') {
    phase = acknowledgedBytes === totalBytes ? 'submitted' : 'unknown';
  } else if (submitState === 'not-sent') {
    phase = acknowledgedBytes === 0 ? 'not-sent' : 'partial';
  } else {
    phase = 'unknown';
  }
  return {phase, automaticResend: false, maySendSubmit: false,
    recovery: {submitted: 'none', 'not-sent': 'confirm-target-and-send-new',
      partial: 'clear-draft-before-new-input', unknown: 'inspect-native-receipt-before-resend'}[phase]};
}
