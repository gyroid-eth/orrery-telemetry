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

全field必須、余分なfield拒否。file0600、state/runtimeと親directory0700。例のname/ID/pathを実際の値へ推測で置換しない。

```json
{
  "kind": "orrery-daemon-binding-v1",
  "name": "BlueLake",
  "agent_id": 123,
  "project_key": "/absolute/project",
  "connection": "/private/connection.json",
  "peer_name": "GreenCastle",
  "peer_id": 124,
  "pause_path": "/private/state/paused.json",
  "lock_path": "/private/state/daemon.lock"
}
```

```bash
<dev-python> <helper-release>/bin/agentstack-daemon inspect --profile <profile.json>
```

startupと各callでmanagement socketの同一UID/private owner credentialと現在のfingerprint/generation、HTTPの同じ永続UUID、whoisのname/数値row/project/program、固定peerを照合する。generation変更は既存helperを止め、operator確認後の再起動を待つ。HTTP redirectは拒否。現在のbundled Mailのread APIがowner-token引数を広告しない場合はlocal-principal transportとhelper側のowner照合が境界で、引数を勝手に加えない。Mail全体にowner別認可を追加した意味ではない。

exportはcall(name,args)、wait_for_notification(timeout)、closeのみ。allowlistはnormalized runtime_status、固定peerのwhois、自分のfetch_inbox、ACK、固定peerへのsend_message。caller/project/token変更、registration/enrollment/retirement、他者inbox、signal/DB読取は不可。sender_id/project_idを保持し、未知sender・別projectはACK前に拒否。数値agent IDはCodex Appのsession/subagent IDと別。

## 通知と停止

既存agentstack-await-reply.wait_for_replyを共通primitiveとして再利用。認証済みbound fetchを注入し、本文無し・最大1,000件の自分のinboxだけを照合する。独自watcher/別transport/signal scannerは作らない。待機はACK/readを変更せず、本文取得・保存・ACKはconsumerの仕事。

本文取得で見た最大IDは一時wait watermarkで、保存cursor/既読ではない。再起動で0へ戻し、通知がなくても最大60秒でconsumerへ戻す。満杯の1,000件窓を全件回収済みと表示しない。二重wait/二重profile起動は拒否する。

pauseは入口・各network直前（metadata待機も含む）で検査。進行中の取消しは保証しない。closeはwait Eventを取消し、待機完了後にlockを解放する。HTTP/control timeoutは各2秒、close上限4.5秒。本文/token/自由な例外文を診断へ出さない。合成bindingと一時HOME/DB/socket/portの実Mail試験はlive受入れと区別し、Keychain/launchd/Tunnel・source公開は別承認。
