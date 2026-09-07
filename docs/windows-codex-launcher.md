Community-maintained / experimental.
検証環境は Windows 11 build 26200、CPython 3.12.10、Windows PowerShell 5.1、Codex 0.153.4、WinGet の `arndawg.tmux-windows` version `3.6a-win32.7`（binary `tmux3.6a-win32`）です。

# Windows Codex child launcher（PR1）

[Issue #14](https://github.com/gyroid-eth/orrery-telemetry/issues/14) に対する PR1 は、事前登録済みの Codex child を専用の native Windows tmux server で起動するための実験的な境界です。
Native Windows は ORRERY Telemetry の supported environment ではありません。
Windows で検証する経路は WSL2 です。
Dashboard の NEW AGENT / SPAWN は引き続き無効であり、この launcher も Dashboard SPAWN を有効にしません。

この launcher は identity の登録、contact の確立、task の配信を行いません。
parent agent は ORRERY Mail で child identity を事前登録し、contact と canonical task を準備してから、以下の command を実行します。

## Launch 前の準備

- native tmux を別途インストールします。
  検証環境では `arndawg.tmux-windows` の `3.6a-win32.7` と binary `tmux3.6a-win32` を使いました。
- `--codex-home` には、既存の child 専用 private directory を指定します。
  ACL は current user SID だけに access を許可しなければなりません。
  この directory に `config.toml` を置かないでください。
  launcher が child 専用 file を書き込みます。
- Codex の authentication、login、first-run の trust と permission setup は、あらかじめ完了させてください。
  launcher は credential の作成や first-run setup を完了させません。
  `[windows] sandbox = "elevated"` の entry は child launch の設定であり、Codex や Windows の authentication と setup を完了させるものではありません。
  trust、sign-in、setup、approval の画面が残っている場合、readiness timeout が launch を停止し、task は注入されません。
- parent が Mail に child を事前登録したら、返された owner token を一行の child token handoff file に保存します。
  handoff file と `--mail-env` の env file にも current-user-only private ACL が必要です。

## Launch

既存の absolute `.exe` path を `--codex` と `--python` に明示して渡します。
`--mail-url` は Mail endpoint、`--mail-env` は transport configuration file の path です。
owner token の値や bearer token を command line、prompt、inline environment に書きません。

```powershell
$Python = 'C:\path\to\python.exe'
$Launcher = 'C:\path\to\orrery-telemetry\scripts\windows\codex_launcher.py'
& $Python -X utf8 $Launcher launch `
  --name 'BlueLake' `
  --parent 'GreenCastle' `
  --cwd 'C:\path\to\project' `
  --project 'project-key' `
  --codex-home 'C:\path\to\prepared-child-CODEX_HOME' `
  --state-directory 'C:\path\to\private-launch-state' `
  --child-token-file 'C:\path\to\private-child-token.handoff' `
  --mail-url 'http://127.0.0.1:18765/mcp' `
  --mail-env 'C:\path\to\agent-mail.env' `
  --bearer-mode 'enabled' `
  --codex 'C:\path\to\codex.exe' `
  --python $Python `
  --model 'gpt-5.6-sol'
```

主な引数は次のとおりです。

| 引数 | 挙動 |
|---|---|
| `--name`、`--parent` | Mail で準備済みの child 名と parent 名です。PR1 では parent のない launch を受け付けません。 |
| `--cwd` | child が作業する既存 directory です。 |
| `--project` | Mail に渡す project key です。 |
| `--codex-home` | ACL を検証した child 専用 `CODEX_HOME` です。既存の `config.toml` は拒否されます。 |
| `--state-directory` | launcher が per-launch state を作る private root です。parent directory は事前に存在しなければなりません。 |
| `--child-token-file` | current-user-only ACL を持つ一行の token handoff file です。成功すると source が消費されます。 |
| `--mail-url` | Mail endpoint です。例では loopback MCP URL を示しています。 |
| `--mail-env` | Mail env file の absolute path です。省略すると `AGENTSTACK_MAIL_ENV` を使います。 |
| `--bearer-mode` | 既定の `enabled` または `disabled` です。`disabled` は proxy environment にある ambient bearer token を削除します。 |
| `--codex`、`--python` | 既存の absolute `.exe` path です。例では両方を明示しています。 |
| `--model` | child に渡す Codex model です。 |
| `--effort` | `low`、`medium`、`high`、`xhigh`、`max` のいずれかで、既定値は `xhigh` です。 |
| `--approval` | `never`、`on-request`、`untrusted` のいずれかで、既定値は `never` です。 |
| `--ready-timeout` | recognized prompt を待つ秒数で、既定値は 90 秒です。 |

launcher は専用の tmux server を起動し、client command を発行する前に server の PID、creation time、executable を記録します。
server が named pipe の準備前に終了すると、launcher は 2 つ目の server を作らずに失敗します。
child が recognized prompt に到達しない場合、pane を private state に保存し、task を送信しません。
trust や first-run setup で人間の操作が必要な場合、launch 時に表示された `tmux_socket` と child name を使って attach します。

```powershell
tmux -S '<tmux_socket>' attach-session -t '<session>'
```

launch result または stderr に示された `tmux_socket` と `session` の値をそのまま使います。
`-S` が socket の指定で、`-L` は使いません。

## Token と state の境界

`--child-token-file` には private ACL があり、regular file でサイズは 4096 bytes 以下、内容は空でない token 一行でなければなりません。
検証後、launcher は token の値を per-launch state の `owner.token` に専用にコピーし、source handoff file を削除します。
token の値は Codex argv と task prompt に現れず、child proxy に渡るのは private な `owner.token` path だけです。

`owner.token` は `stop` または child の自然終了時に削除されます。
この cleanup は Mail identity を retire しません。
parent が準備した identity、contact、task history は parent の責任として残り、launch failure result は登録が保持されたことを報告します。

Codex child process は kill-on-close Windows job に割り当てられます。
Codex が先に終了して MCP process や別の descendant が残っている場合、job を close するとその descendant を終了します。
job の外側にある process は残ります。

`run_codex_proxy.py` は private な `AGENTSTACK_MAIL_ENV` file を一行ずつ読みます。
file を shell script として評価しないため、env-file の内容は command として実行されません。
`--bearer-mode enabled` では `HTTP_BEARER_TOKEN` が必要で、`disabled` では ambient な `MCP_AGENT_MAIL_TOKEN` を削除します。

## Stop

launch を stop するには、launch result の `state_directory` に返された exact な per-launch path を使います。
parent state root を推測しません。

```powershell
$State = 'C:\path\to\private-launch-state\<state-directory-returned-by-launch>'
& $Python -X utf8 $Launcher stop --state-directory $State
```

stop operation は、記録された PID、creation time、executable が一致する process と、その process が所有する descendant だけを対象にします。
無関係な user process を残し、user sandbox や junction target を再帰的に削除しません。
reparse point を含む path は private state として拒否されます。
PID が再利用された場合や creation time または executable が一致しない場合、その process は終了させません。

## Readiness と検証範囲

Readiness は、表示された model を含む idle Codex prompt を認識します。
startup、sign-in、trust、setup、approval、usage-limit の画面は blocked state です。
人間の操作が必要な画面を自動で確認済みにすることはなく、timeout は fail closed します。

focused regression run では `tests/windows/test_codex_launcher.py` の 15 tests が passed しました。
このテストは native-Windows ACL、token handoff、proxy の bearer environment 処理、owned-process cleanup、kill-on-close job、PID reuse protection、readiness、runtime environment propagation を検証します。
この結果は real smoke run とは別に記録されます。
installer 全体、Codex App Bridge、Dashboard SPAWN、Mail history、native Windows 全体の成功を示すものではありません。
