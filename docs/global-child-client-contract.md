# global 子 client の契約表

[利用手順](global-child-client.md) / [English guide](global-child-client.en.md)

正本: `packages/agentstack_mail/fixtures/global-client-4b.json`。v2 の講評から、期限の読取と閾値付き renew、応答喪失後の同 UUID replay を実装に反映しています。実起動・proxy・wake は4c待ちです。

## 呼び手と tool

| 呼び手 | 旧 tool | 新 tool |
| --- | --- | --- |
| bin/agentstack-preregister-child | ensure_project / whois(name) / register_agent / set_contact_policy | register.sh → register_agent(same pending) / NAME_CONFLICT时resolve_agent_identity(parent) + whois(child) / explicit set_contact_policy |
| hooks/spawn_child.sh direct / --pre-registered | register_agent / whois / unretire_agent / file_reservation_paths / send_message | register.sh child operation + register_agent / owner inspect / unretire_agent / file_reservation_paths / send_message |
| hooks/spawn_gemini_child.sh / spawn_gemini_preregistered.sh | 旧 helper の register / reservation / report / retire | register.sh の child 操作 + file_reservation_paths / send_message / retire_agent |
| bin/agentstack-gemini-child-mail | reserve_files / release_reservations / send_message / retire_agent (legacy client methods) | file_reservation_paths / release_file_reservations / send_message / retire_agent |
| hooks/child_resume.py stage_registration / prepare_active_state / begin_resume | 直接 tool 無し、project/name/token を含む retained state | RuntimeClient reference + register.sh child operation |
| hooks/child_tools.py / generated Claude MCP・Codex HOME | proxy config に旧 project/name/token path | 4c 用の固定 launch handoff (公開 tool 追加無し) |
| hooks/cleanup-child-agent.sh | release_file_reservations / retire_agent + name-keyed token deletion | retire_agent (S2c の ACTIVE 解放に委譲) / management inspect |
| bin/agentstack-purge-child-resume / expiry maintenance | child_resume.purge_one (local), 旧退役照合 | common child inspect/purge (local) + management inspect |
| dashboard/server.py do_resume / _register_claude_resume / _resume_registration | DB の project/name SELECT / register_agent / unretire_agent / retire_agent | register.sh の child 操作 / management inspect / unretire_agent / whois / refresh_registration |
| dashboard/server.py _do_resume_codex / bin/agentstack-resume | /api/jump session=name / shell bootstrap register_agent | /api/jump explicit client_name / common resume adapter |
| bin/agentstack-codex-bootstrap / provider resume wrappers | register_agent(name/project) / codex resume SESSION_ID | common mode/owner + refresh_registration or touch_window_identity / provider resume SESSION_ID |
| hooks/mark-agent-registered.sh / check-agent-registered.sh | register_agent posttool を自己登録の flag とする | whois + existing schema3 self session index |
| hooks/session-start-reminder.sh | health_check / reregister(register_agent) | common health + observe/refresh, capability による touch |
| hooks/check-file-reservation.sh | renew_file_reservations paths=[relative,absolute] | check_file_reservations(absolute) → renew_file_reservations(explicit owned IDs) |
| hooks/release-file-reservation.sh / worker / release-all-reservations.sh | release_file_reservations(relative paths or omitted) | release_file_reservations(checked IDs or normalized absolute pattern) |
| skills/delegate/SKILL.md | 旧 preregister argv / reserve / contact / send / await / retire | global CLI contract + actual bound tools; file_reservation_paths / contact / send / retire |
| skills/log/SKILL.md | Mail tool 直接利用無し、AGENTSTACK_PROJECT_KEY を vault destination に利用 | Mail tool 追加無し、filesystem destination を Mail routing から独立 |
| integrations/codex_app/plugin/skills/coordinate/SKILL.md | session-bound bootstrap / proxy tools | 同じ bound tool schema (global proxy 実結線は4c) |
| claude/CLAUDE.md / codex/AGENTS.md / embedded prompt | raw project/name tools と proxy route の案内 | mode-aware helper / global owner context / stable peer IDs |
| scripts/canary-embed-task.sh / dashboard spawn registration writer | 旧 preregister / send / retire / registration SELECT | 共通 preregister/lifecycle adapter |

## 中断と再開

| ID | 元の状態 | 操作 | 次の状態 | 戻り方 |
| --- | --- | --- | --- | --- |
| T01 | context 未選択 | 共通 mode selector、明示/implicit context の private/binding 確認 | legacy または global-preflight | 不明/broken symlink/途中変更は拒否。旧 transport へ戻らない。 |
| T02 | global-preflight | capability・4c 実行可否・入力・固定 root/全 slot・provider/作業folder/tools/task を確認 | prepared child root(identity=null) | 通常 spawn の4c不足はここで止める。明示 prepare-only に限り登録へ進む。 |
| T03 | prepared child root | 4a登録 pending に token/name/program/model/task を原子的保存 | registration planned | 中断後も同じ name/token/metadata/binding を register(new) へ再送。新しい値で再登録しない。 |
| T04 | registration planned | 同じ保存intentでS1 register(new)。NAME_CONFLICT時だけ親resolve→子token whois→owner照合。 | registered row or registration unknown | 未送信/既着/遅延を同じ再送で扱う。通信不明は保持して同intent再試行。確定した別ownerだけoperator例外。 |
| T05 | registered row | credential → context → metadata の既存4a順で保存 | identity active; child-state未完成でも可 | 各保存後中断は4a explicit finalize-create 同IDで再開。新 token/name/root を作らない。 |
| T06 | identity active | child-state/tools/task 確定、明示 policy/予約/task-mail があれば共通 mutation | prepared | 途中は固定 lifecycle intent を再提示、message/予約は同UUID。embedded は task-mail なし。 |
| T07 | prepared | 4c capability/launch handoff確認→tmuxの新paneへ唯一のlaunch_idを付与 | launch pending | 4c無しはready=falseで停止。tmux起動前・後中断は同launch_idのpane/runtime receiptを観測、blind再起動しない。 |
| T08 | launch pending | 専用 tmux/server/process証跡の再観測 | running / prepared / launch unknown | matching pane ありは二重起動禁止。確実に未実行は同準備から再開。unknownは非破壊inspectで止める。 |
| T09 | running | SessionEnd/cleanup共通endで別sessionのlive/unknownを先に判定、最後のchildならretireのみ。 | retire pending → retained | live/unknownなら解放もretireも無し。retire応答喪失はinspectで現在status確認、owner material保持。 |
| T10 | retained retired | root/provider/session/retention/tools/authorityとcredential fingerprint照合、resume intent保存 | resume planned | retired whoisは使えないので先に管理inspect。legacy rootを変換しない。 |
| T11 | resume planned | inspect target status→必要ならunretire→whois→refresh/touch/verify | resume identity active | unretire応答喪失は管理inspect。token/ID/generation維持、明示recover/claimは実行しない。 |
| T12 | resume identity active | 同じprovider session IDとtoolsから派生HOME/configを準備→4c handoff | launch pending | 部分HOME生成は同固定派生dirだけ再生成。確定した未execは明示cancelで退役へ、exec unknownは戻さない。 |
| T13 | retire pending / resume planned / launch unknown | inspect-child(client_name) | 同状態・読取結果 | 結果を3値で提示。DB/OS未観測をfalseとみなさない。固定local operation lock内で照合。 |
| T14 | retained・期限切れ | pending無し/実行無し/retired確認→固定生成物削除 | purged generated state | credential/root/historyの破棄は別の明示forget。途中削除はmanifestに固定slot結果を記録し同操作再開。 |
| T15 | any pending | pending種別別: registration同intent再送、mutation既存resolve-mutation、launch同launch_id観測/明示cancel。 | blocked with preserved evidence | 結果不明は証拠保持。S2a resolve-mutationをregistration/launchへ渡さない。binding変化は自動repin/UUID再生成/旧fallback禁止。 |

## 受け入れ

| ID | 領域 | 入力と期待 |
| --- | --- | --- |
| A01 | mode | 旧 env project/name/token と明示global contextが食い違ってもglobalの同ID。broken implicit symlinkは旧登録/flagを作らない。 |
| A02 | preflight | 任意token-file/profile/root/session namespace入力、他root/symlink/hardlink/DB入力はnetwork前拒否、bytes/revision不変。 |
| A03 | register | 実 preregister の --program/--model/--task-description がrowへ。stdoutにtoken無し、生成名競合はNAME_CONFLICTのみで判断。 |
| A04 | register-unknown | 同じname/token/metadata/bindingの未送信・既着・遅延両順序をregistration_retry_contract表で固定。各rowは1件、同ID/tokenで有効化。 |
| A05 | activation | credential/context/metadataの各保存直後で中断→正規finalize同ID/token/generation、register回数1。 |
| A06 | registration-mismatch | 他owner競合は子token whoisで拒否、通信失敗は同pending保持。別binding/偽resolve応答をownerとして採用せず、新token/nameを作らない。 |
| A07 | local-finish | child-state/task/tools保存前後中断→同intent再開、schema不適合は未完了。1100 provider filesでも全入口動作。 |
| A08 | policy | 未指定policyのreconnectは書込無し。明示policyだけUUID mutation、quota/pendingを勝手に解消しない。 |
| A09 | launch | 通常spawnの4c不足は登録前拒否。prepare-onlyはready=false。tmux前/後の中断で同launch_id再照合、unknownで二重起動/retire無し。 |
| A10 | provider | Claude/Codex/Geminiの実CLI argvをstubで確認、元provider session ID維持。native Windows/Gemini stdioの固定reason維持。実LLM/別版binary無し。 |
| A11 | rename | 表示名変更後もclient_name/root/agent_id/transcript不変、親の送信は保存stable ID。name-keyed旧ファイルを探さない。 |
| A12 | resume | retired inspect→unretire前/応答喪失/後→whois→metadata/派生HOME→tmux/exec各点。明示cancelは確定未execのみ、unknownは証跡保持。 |
| A13 | resume-mismatch | provider/session/header/ID/gen/window/instance/tools破損拒否。expired windowは同mapping touch→verify、新規mappingはreadyにしない。 |
| A14 | cleanup | 実SessionEnd→cleanup、逆順、重複のA/B同ID連鎖。別session live/ps・tmux unknownは予約不変、最後childだけretire、expiredはreleased NULL維持。 |
| A15 | hooks-self | PostToolUse register childが親のschema3 index/flag/titleを変えない。startup/resume/compact/clear実payload、false旧flag拒否。 |
| A16 | hooks-path | Edit/Write/MultiEdit等の実payload、2cwd相対名/同absolute/symlink/未作成/glob/Unicode/空白/WSL。coverageはserver check→ID renew、relative+absolute二重送信無し。 |
| A17 | hook-race | grace workerに昔のcontext/owner/epoch/lease ID→新contextの予約を解放しない。再予約後のworkerはlocal generationで無効化。 |
| A18 | release | PostToolUse duplicate path release no-op。最後standaloneの101以上ACTIVEをkeyset+100ID batch、他owner履歴無視。中断は同UUID、別session稼働なら全件保持。 |
| A19 | message-intent | helper send/reportのbody/base64/宛先IDsをlocal単一intentに保存。HTTP前中断とcommit応答喪失→再起動だけで同bytes/UUID replay、別intent拒否。 |
| A20 | shared-contract | 全31toolのcatalog/入力・出力schema/phase拒否表を正本からcompile。REQUEST_ID_CONFLICT/未知理由/schema不適合でpending保持。definitiveのみ解除。 |
| A21 | retired-replay | read/write current owner認証が必要。retired/generation変更はpending保持、unretire後同UUIDのhistorical receiptは現在stateと区別。 |
| A22 | skill | delegateの旧argvと新globalコマンドを抜き出して実shell stubへ。logはMail project無しで保存先を選ぶ。proxy skillは実schema以外のcaller fieldを足さない。 |
| A23 | legacy | 旧token-file-out/name/stdout/retained schema/argv、既存docstringの冪等・cancel・専用HOME・tools保持を既存試験で確認。globalの新formatを旧へ二重保存しない。 |
| A24 | layout | client root 固定親/name、child slot追加も全preflightに1回登録。rootは1100files/130dirs/history増加でもbounded scan無し。 |
| A25 | installer | fresh core / full / provider-preserving / dashboard-only / archive切出し / dry-runで宣言正本、helper/fixture bytes一致。 |
| A26 | sqlite | client/child/hook/dashboard global branchでsqlite3.connect/DB os.open/close/hash/copyゼロ。S2cの別processwriter lock回帰も実serverで維持。 |
| A27 | health | signal.status redでもtop-level okならtoolを呼べる。auth/authority不明はreadyを立てない。HTTP health一致までfixtureがyieldしない。 |
| A28 | purge | pending/unknown/live sessionならcredential/history保持。期限切れの正常retained生成物だけ対象、partial purge→同fixedslotで再開。 |
| A29 | outage-real-hooks | tests/test_check_file_reservation.py の実Edit/Write payloadをlegacy/globalへ。初期connection refused/timeout × 未指定/明示warn-open/block。0+既存warning/audit、2+拒否をoutage_contractに一致。 |
| A30 | outage-classification | 同payloadでcontext/owner/binding不正・401/403/5xx・MCP isError/schema不適合・definitive zero後retry outageを与える。どちらのpolicyでも既存拒否、pending非破壊。 |
| A31 | posttool-real-call | tests/test_release_file_reservation.py のsuccess/tool_response/file_path/cwd/session_idとgrace worker実argvで通信断。edit巻戻し無し、exit0+監査、warn-open既存警告、unknown解放を成功扱いしない。別sessionendとは共通判定。 |
| A32 | single-registration-frontdoor | source5入口(preregister/reregister/codex-bootstrap/agent-start/spawn)とdashboard JSON entrypointを追跡。全global dispatchがregister.shを通り、同じRuntimeClient処理、legacy byte/argv/stdout維持。 |
| A33 | caller-inventory | 基準grep48fileとsample/test9行、間接入口をinventory表と照合。未分類0、各changed/retained/deferredの根拠。 |
| A34 | renew-budget | 連続N編集で期限閾値までreceipt増加0、閾値以下でrenew1回、その後増加0 |
| A35 | renew-replay | renew応答喪失→次の実PreToolUseで同UUID replay→receipt1件、coverage確認 |
