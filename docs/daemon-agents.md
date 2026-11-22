# 決定的daemonの正規bindingと通知

[English](daemon-agents.en.md)。local実装。一般installerはこのCLIを配備しない。固定source releaseの `bin/agentstack-daemon` を開発用Pythonで実行する。通常のClaude/Codex profile・proxy schema・startup ritualは変更しない。

## 専用identityをoperatorが一度作る

connectionは既存 `orrery-mail-connection-v1` のowner-private copy。UUIDは[enrollment inspect/pin](persistent-agents.md)でoperatorが確認した値。literal loopback HTTPのみ、http_bearer_modeはdisabledのみ。HTTP health_check.server_instance_id対応の新Mail buildが必要で、古いbuildは拒否する。環境のcredentialや別URLへfallbackしない。

```bash
<dev-python> <helper-release>/bin/agentstack-daemon create \
  --connection <0600-connection.json> --project-key <exact-project> \
  --name <operator-confirmed-free-canonical-name> \
  --journal <private-directory/create.json> --operator
```

parentが既存bound whoisで候補名を確認し、operatorがname/用途/projectを確認する。既存identity・alias・LLM providerを流用しない。createはMCP/proxy catalogに無い。operator flagは人間の本人証明ではなく、同一OS userを信頼するlocal境界と、人が実行する運用規約である。

owner tokenを生成し、0600/fsyncのjournalを保存してから正規register_agentを一度だけ呼ぶ。program=orrery-daemon、model=deterministic。返るname/正の数値ID/tokenの一致を確認し、connectionのruntime_dirにagent_token_<name>を排他的に保存する。既存credentialは上書きせず、stdoutは非秘密receiptだけ。

応答不明/部分保存/不一致では止める。journalの削除・別名の作成・register再送はしない。operatorが正規whoisで同じproject/nameの数値IDを確認した場合だけ、同じjournalを回収する。

```bash
<dev-python> <helper-release>/bin/agentstack-daemon finalize-create \
  --connection <same-connection.json> --journal <same-create.json> \
  --agent-id <operator-confirmed-id> --operator
```

managementのUUID/row/name/project/retirement/fingerprintとHTTPのUUID/row/programを照合して同じcredentialを保存する。register/claim/recover/retireは呼ばない。異なるrow/tokenは拒否。rowを確認できなければjournalを保持してoperatorへ戻す。journal/credentialの中身をモデルに見せない。

## profileとruntime

全field必須、余分なfield拒否。`peers` は相手の一覧で、各要素はnameと正の整数idの2つだけ（1〜16件、name/IDの重複と自分自身は拒否）。nameはMailの正規形（ASCII英字で始まる英数字1〜128文字、`NAME`）。同じ一覧をpinするconsumerも同じ規則を使う。単数のpeer_name/peer_idを持つv1 profileは読まず `profile-legacy-v1` で止まる。operatorがv2へ1回書き換える。file0600、state/runtimeと親directory0700。例のname/ID/pathを実際の値へ推測で置換しない。

```json
{
  "kind": "orrery-daemon-binding-v2",
  "name": "BlueLake",
  "agent_id": 123,
  "project_key": "/absolute/project",
  "connection": "/private/connection.json",
  "peers": [
    {"name": "GreenCastle", "id": 124},
    {"name": "RedRiver", "id": 125}
  ],
  "pause_path": "/private/state/paused.json",
  "lock_path": "/private/state/daemon.lock"
}
```

```bash
<dev-python> <helper-release>/bin/agentstack-daemon inspect --profile <profile.json>
```

startupと各callでmanagement socketの同一UID/private owner credentialと現在のfingerprint/generation、HTTPの同じ永続UUID、自分のwhoisのname/数値row/project/programを照合する。peerの数値row（project・退役も）は、whoisとsend_messageの直前にその対象のpeerだけを照合する。`inspect` は一覧の全peerを照合する。generation変更は既存helperを止め、operator確認後の再起動を待つ。HTTP redirectは拒否。現在のbundled Mailのread APIがowner-token引数を広告しない場合はlocal-principal transportとhelper側のowner照合が境界で、引数を勝手に加えない。Mail全体にowner別認可を追加した意味ではない。

exportはcall(name,args)、wait_for_notification(timeout)、closeのみ。allowlistはnormalized runtime_status、一覧のpeerのwhois、自分のfetch_inbox、ACK、一覧のpeer 1人宛てのsend_message（送信直前にそのpeerの数値rowをwhoisで確認し、不一致なら `daemon-peer-changed`、Mailがそのagentは無いと答えれば `daemon-peer-missing`）。missingの判定はclientの `AgentMailClient.whois` の1か所で、Mailのnot-found定型文面だけを読む（Mailは種別を文面でしか返さないため。native Mailの契約試験で固定し、文面が変わればoutage側に倒れる。Mailが種別を構造化して返すようになったら種別に置き換える）。この2つだけが「そのpeerだけ使えない」を表す固定codeで、`PEER_STATUS` に定義する。consumerはこのmapだけで判定し、Mailの例外文面は照合しない。他の失敗（Mailへ届かない等）は `daemon-operation-failed` のまま障害として扱う。caller/project/token変更、registration/enrollment/retirement、他者inbox、signal/DB読取は不可。sender_id/project_idを保持し、一覧外のsender・別projectはACK前に拒否。一覧のnameで数値IDが違うMailは拒否せずそのまま返し、consumerが保存・ACKせず保留する。1人の入替で他のpeerの受信を止めないため。数値agent IDはCodex Appのsession/subagent IDと別。

## 通知と停止

既存agentstack-await-reply.wait_for_replyを共通primitiveとして再利用。認証済みbound fetchを注入し、本文無し・最大1,000件の自分のinboxだけを照合する。独自watcher/別transport/signal scannerは作らない。待機はACK/readを変更せず、本文取得・保存・ACKはconsumerの仕事。

本文取得で見た最大IDは一時wait watermarkで、保存cursor/既読ではない。再起動で0へ戻し、通知がなくても最大60秒でconsumerへ戻す。満杯の1,000件窓を全件回収済みと表示しない。二重wait/二重profile起動は拒否する。

pauseは入口・各network直前（metadata待機も含む）で検査。進行中の取消しは保証しない。closeはwait Eventを取消し、待機完了後にlockを解放する。HTTP/control timeoutは各2秒、close上限4.5秒。本文/token/自由な例外文を診断へ出さない。合成bindingと一時HOME/DB/socket/portの実Mail試験はlive受入れと区別し、Keychain/launchd/Tunnel・source公開は別承認。
