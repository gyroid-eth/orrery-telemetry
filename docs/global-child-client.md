# global client の子・再開の準備

[English](global-child-client.en.md)

この経路は隔離した global candidate の明示的な準備用です。既定は旧 mode、`activation_enabled=false` のままです。公開 Mail tool は既存31個から増えません。実行・proxy・wake は後続の4cで対応します。

## 入口と固定 layout

登録・子・resume の shell 入口は `bin/lib/agentstack-register.sh` の1本です。各 launcher は旧 env を読む前に明示 context を確認します。context が無ければ旧動作、不正・壊れた symlink は拒否です。global で name/project/token file の旧探索へ戻りません。

`AGENTSTACK_CLIENT_CONFIG` は親の固定 `runtime-client.json` を指します。子は同じ `agentstack/clients/` 直下の別の `client_name` へ置きます。資格情報は既存の `credential.json` のみです。`runtime/child-state.json` は親の stable ID・task/tools・準備の状態だけを持ち、子の token や identity のコピーを持ちません。任意の token/profile 出力先は受け付けません。新 identity は新しい root を使います。

```bash
agentstack-preregister-child --child-client-name child_01 --name ChildAlpha \
  --program codex --model explicit-model --task-description '作業の要約' --prepare-only
bash "$AGENTSTACK_HOME/hooks/spawn_child.sh" --child-client-name child_01 \
  --embed-task --task-file task.md --prepare-only
agentstack-resume --client-name child_01 --prepare-only --detached
```

返り値の `prepared=true` は保存と認証の準備が済んだことです。`runtime_ready=false` は継続し、通常の spawn/resume は副作用前に `GLOBAL_CHILD_RUNTIME_REQUIRES_PR4C` で止まります。新しい window mapping を作りません。既存 mapping だけ touch/verify します。provider 起動・HOME生成・tmux後の状態の検証は4cで行います。

## 登録が途中で止まった場合

再開は同じ preregister コマンドです。既存 pending に保存した同じ name/token/program/model/task/binding だけを再送します。未送信なら作成、既着なら `NAME_CONFLICT` 後に親の resolver と子 token の whois で同じ row を確認します。遅れて届いた要求も名前の原子的な重複拒否で1行に保たれます。衝突だけで pending を消しません。

credential → context → metadata の各保存後に止まっても、同 pending で有効化を終えます。通信・binding 不明を他 owner と解釈しません。外部 operator が未完了の名前を rename/delete する場合は、この再送の前提が変わるため先に pending を確認してください。

確定した他 owner の競合だけは、子の context を明示して `inspect-child` と `abandon-child-registration --operator --expected-digest SHA256` を使います。後者は pending の digest と現在の owner 拒否を確かめ、同じ pending を terminal にします。別人を retire せず、資格情報を消さず、再登録は新 root で行います。書込みの成功後の disk 失敗は rollback と呼ばず、pending に残して再開します。

```bash
bash "$AGENTSTACK_HOME/bin/lib/agentstack-register.sh" inspect-child --context CHILD_CONTEXT
bash "$AGENTSTACK_HOME/bin/lib/agentstack-register.sh" finalize-child \
  --context CHILD_CONTEXT --operator --agent-id ID
```

## hook と終了

PreToolUse は絶対 path の coverage と自己 ACTIVE の期限を read で確認します。更新閾値は client の `RENEW_THRESHOLD_SECONDS=600` の1か所です。残り600秒以下だけ1800秒の renew を送ります。連続編集だけで receipt を増やしません。renew の応答を失ったら編集は拒否し、次の PreToolUse が同じ pending/UUID を先に replay します。別の mutation pending を勝手に実行しません。

既定/明示 `warn-open` の初期 transport 通信断は、旧 hook と同じ警告・監査付きで編集を許可します。拒否の実設定値は `AGENTSTACK_MAIL_OUTAGE_POLICY=block` です。context/owner/binding/schema 不正、HTTP/MCP の拒否、結果不明の renew は warn-open の対象外です。PostToolUse は完了済み編集を戻せず、失敗を監査します。grace worker は固定 slot の世代と捕捉済み lease ID を照合し、新しい予約を path で選び直しません。

SessionEnd と cleanup は同じ end 操作です。同 ID の別 session が live または ps/tmux の結果が unknown なら、解放も退役もしません。最後の child は server の retire 1本（ACTIVE の解放を含む）、最後の standalone は自己 ACTIVE の release だけです。実行の証拠は固定 `runtime/live-sessions/` に保存し、予約の session 別所有表は作りません。provider PID の出生時刻が確認できなければ unknown を保ちます。

保管期限後の `agentstack-purge-child-resume` は、未完了 pending が無く退役済みと確認できる場合に固定の生成 task だけを消します。credential・self session 記録・provider transcript は保持します。

## 旧 mode との差と未対応

| 項目 | 旧 mode | global の準備経路 |
| --- | --- | --- |
| 登録 | project/name・token file 出力 | 固定 context/credential、stable ID |
| stdout | 子の名前 | token を含まない準備 JSON |
| 予約更新 | 編集ごとに renew | 期限閾値以下だけ renew、同 UUID 再送 |
| SessionEnd | 旧 hook 連鎖を維持 | 共通 end で別 session を先に確認 |
| 通信断 | 既定 warn-open / block | 同じ policy、認証等の拒否と区別 |
| 新 window / 実行 | 既存 launcher | unmapped / 4c 待ちの固定拒否 |
| 旧保管形式 | 既存の形式 | 自動変換せず PR7 の明示 import |

Gemini stdio/proxy・native Windows は既存の未対応理由を維持します。WSL は共通 Unix client を使います。配送/wake は4c、managed block移行はPR5、画面はPR6、利用者向け切替とreleased形式のimportはPR7です。

## 確認するもの

正本は `packages/agentstack_mail/fixtures/global-client-4b.json` です。v2案からの差分は閾値付き更新と次回hookの replay です。呼び手48 file＋補助9 hit＋間接12、35受け入れを記録しています。実 HTTP fixture は health/profile/binding が一致するまで待ちます。旧 mode・bash3.2・installer の切出しと配布 bytes も関連試験に含めます。client は Mail DB を開閉・hash・copy しません。

[呼び手・中断・受け入れの表](global-child-client-contract.md)は fixture から生成しています。grace worker の失敗は既存の固定 release slot の `last_error` に残します。fence 不正時はこの保存も拒否します。resume・期限後の削除も end と同じ生存確認を使い、live/unknown の session があれば進めません。

resume intent は unretire の前に保存します。応答喪失時は管理 inspect から同じ ID/token で再開します。期限切れは `CHILD_RETENTION_EXPIRED`、purge 済みは `CHILD_PURGED` で拒否します。確定した未実行の `cancel-child-resume --operator` は、live/unknown が無いと確認して共通 retire に戻ります。
