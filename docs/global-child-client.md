# global client の子・再開の準備

実起動・proxy・window binding・配送は [global delivery client](global-delivery-client.md) を参照してください。このガイドは子の準備を扱います。既定は旧 mode のままです。

[English](global-child-client.en.md)

この経路は隔離した global candidate の明示的な準備用です。既定は旧 mode、`activation_enabled=false` のままです。準備だけでは provider を起動しません。明示的な実行は起動・配送のガイドに従います。

## 入口と固定 layout

登録・子・resume の shell 入口は `bin/lib/agentstack-register.sh` の1本です。通常の context 判定は `runtime_client.selected(argv)` の1関数で、共通 loader が register の入口へ渡します。各 launcher は旧 env を読む前に判定します。古い partial install で helper が無い、または `select` を呼べない場合だけ、loader が context の有無を確認し、あれば `GLOBAL_ENTRY_UNAVAILABLE`、無ければ判定用の Python を起動せず旧経路です。この context 有無の条件は loader の1定義を共有します。Mail 無効化の skip は global を選択した後・owner material を読む前に行い、旧 hook の警告と終了値は維持します。context が無ければ旧動作、不正・壊れた symlink は拒否です。global で name/project/token file の旧探索へ戻りません。wrapper と loader は同じ配布単位で、loader 自体の欠落は不完全な配布として拒否します。installer は配備前に同じ契約ファイル一覧で欠落を確認します。

`AGENTSTACK_CLIENT_CONFIG` は親の固定 `runtime-client.json` を指します。子は同じ `agentstack/clients/` 直下の別の `client_name` へ置きます。資格情報は既存の `credential.json` のみです。`runtime/child-state.json` は親の stable ID・task/tools・準備の状態だけを持ち、子の token や identity のコピーを持ちません。任意の token/profile 出力先は受け付けません。新 identity は新しい root を使います。既存 standalone の root を子に転用すると `CLIENT_ROOT_NOT_CHILD` で拒否します。既存 child は同じ親と登録 intent の digest を照合し、異なる name/program/model/task は `CHILD_REGISTRATION_INTENT_CONFLICT` で拒否します。token や payload の別コピーは持ちません。

```bash
agentstack-preregister-child --child-client-name child_01 --name ChildAlpha \
  --program codex --model explicit-model --task-description '作業の要約' --prepare-only
bash "$AGENTSTACK_HOME/hooks/spawn_child.sh" --child-client-name child_01 \
  --embed-task --task-file task.md --prepare-only
agentstack-resume --client-name child_01 --prepare-only --detached
```

`prepared=true` は保存と認証の準備が済んだことです。その時点では `runtime_ready=false` です。通常の spawn/resume は続いて固定 launch の handoff に入り、新 window を stable agent ID に原子的に束縛し、既存 window は照合します。正規の provider session の証拠が揃って初めて ready になります。

### R3 shell の full caller（接続途中）

既に preregister 済みの固定 child に、明示 configure 済みの intent がある場合は、
`spawn_child.sh` の同じ global 入口で caller 全欄を照合します。例えば次の形です。

```bash
bash "$AGENTSTACK_HOME/hooks/spawn_child.sh" --child-client-name child_01 \
  --name ChildAlpha --codex --model local:example --parent ParentAlpha \
  --base mail-only --resources 'Scratch.md' --detached \
  --task-file /absolute/task.md --prepare-only /absolute/workdir
```

name/model/task-file/cwd は full 入力では必須です。provider は `--codex` / `--program`
または `--provider` の CLI 指定、親は固定 parent client から決まり、`--parent` は
その名前への一致の主張です。名前から別の root/credential を検索しません。
resources は繰り返し可能なカンマ区切り、tools は既存の `--tools` 文法を使います。
clear-tools/worktree/chrome は既定 false、detached は従来の global 起動と同じ true
（`--no-detached` で false）です。base の既定は default、tools/resources は空です。
`--effort`、`--strict-output-cap` / `--no-strict-output-cap`、`--task-delivery` は省略を
保ちます。owner の値で欠けた入力を埋めず、相違は task 書込みや R2 の起動前に拒否します。
これらの新しい欄を、共有 parser の別操作へ渡すことも拒否します。

従来の最小の task 参照は残ります。保存 intent があるときは task path/内容・親・
child-state の互換投影も照合し、同じ child mutex 内で再確認してから、R2 へ digest を
引き継ぎます。元 task は configure と同じ owner/regular/no-link/非他者書込の契約で
読み、0644 も受理します。private な保存先の権限は緩めません。最小参照で task-file を
省略したときは、元ファイルを再読せず承認済み保存コピーと delivery を使います。
task-file を明示したときは保存コピーで代用せず、指定元の path/内容を照合します。
preregister と spawn の分担は変えません。通常起動は native の承認を
追加せず、未検証 CLI は `LOCAL_MODEL_ADAPTER_UNSUPPORTED` のままです。
この段は入力と準備の接続であり、配送用 TEST binding の成功を native 権限、通常の
detached/tmux、非 default field の実副作用の受入とは数えません。legacy 入口は従来どおりです。

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

PreToolUse は絶対 path の coverage と自己 ACTIVE の期限を read で確認します。更新閾値は client の `RENEW_THRESHOLD_SECONDS=600` の1か所です。残り600秒以下だけ1800秒の renew を送ります。連続編集だけで receipt を増やしません。mutation の応答を失ったら編集は拒否し、次の PreToolUse が種類を問わず保存済み intent を同じ UUID で先に replay します。確定拒否だけが共通の拒否表に従って pending を解消し、結果不明は編集を止めます。

既定/明示 `warn-open` の初期 transport 通信断は、旧 hook と同じ警告・監査付きで編集を許可します。拒否の実設定値は `AGENTSTACK_MAIL_OUTAGE_POLICY=block` です。context/owner/binding/schema 不正、HTTP/MCP の拒否、結果不明の renew は warn-open の対象外です。PostToolUse は完了済み編集を戻せず、失敗を監査します。grace は旧設定 `AGENTSTACK_RELEASE_GRACE_SECONDS`（fallback: `FILE_RESERVATION_RELEASE_GRACE_SECONDS`）、既定90秒です。`runtime/release-debounce/<lease-id>.json` の slot は別 file の編集で上書きしません。worker は世代・捕捉済み lease ID・revision・期限を照合し、遅延中に更新・再取得された予約を解放しません。`AGENTSTACK_MAIL_DISABLED=1` は hook の context/owner 読込みと通信より先に skip します。

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
| 新 window / 実行 | 既存 launcher | 固定 launch handoff・stable ID binding・正規 session receipt |
| 旧保管形式 | 既存の形式 | 自動変換せず PR7 の明示 import |

Gemini stdio は共通 proxy を使います。Gemini の対話型 root は正規の native receipt が無いため固定拒否です。native Windows は Unix 契約の対象外、WSL は共通 Unix client を使います。managed block移行はPR5、画面はPR6、利用者向け切替とreleased形式のimportはPR7です。

## 確認するもの

正本は `packages/agentstack_mail/fixtures/global-client-4b.json` です。v2案からの差分は閾値付き更新と次回hookの replay です。呼び手48 file＋補助9 hit＋間接12、35受け入れを記録しています。実 HTTP fixture は health/profile/binding が一致するまで待ちます。旧 mode・bash3.2・installer の切出しと配布 bytes も関連試験に含めます。client は Mail DB を開閉・hash・copy しません。

[呼び手・中断・受け入れの表](global-child-client-contract.md)は fixture から生成しています。grace worker の失敗は既存の固定 release slot の `last_error` に残します。fence 不正時はこの保存も拒否します。resume・期限後の削除も end と同じ生存確認を使い、live/unknown の session があれば進めません。

resume intent は unretire の前に保存します。応答喪失時は管理 inspect から同じ ID/token で再開します。期限切れは `CHILD_RETENTION_EXPIRED`、purge 済みは `CHILD_PURGED` で拒否します。確定した未実行の `cancel-child-resume --operator` は、live/unknown が無いと確認して共通 retire に戻ります。

新規 global 起動の専用 socket、runner/provider の区別と終了後の `target` / `replay-end` は [配送 client](global-delivery-client.md#専用起動と終了の回収) の契約を使います。SessionEnd だけで実 process の終了を決めず、未確認報告を残したまま退役しません。
