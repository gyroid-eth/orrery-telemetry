# WSL 自動保持・再起動の実機確認 / WSL hold and restart acceptance

Windows 11 / WSL2 の試験用 Ubuntu を使います。systemd と Windows interop を有効にし、従来モードで Mail と dashboard を導入します。再現時と同じ条件にするなら、作業を保存してから `.wslconfig` の負の idle timeout を試験中だけ外し、次に `wsl --shutdown` で設定を反映します。dashboard のページは閉じておきます。以下の port、unit 名、リポジトリ位置は試験環境に合わせます。

Use a test Ubuntu distro on Windows 11 / WSL2 with systemd and Windows interop enabled. Use a legacy-mode install of Mail and the dashboard. To match the original reproduction, save all WSL work, temporarily remove any negative idle timeouts from `.wslconfig` and apply the change with `wsl --shutdown`. Close dashboard pages. Adjust the port, unit names and checkout location for the test installation.

## Windows 側の起動関数 / Windows launcher

Windows 上の PowerShell で定義します。Windows OpenSSH 経由でも `Win32_Process.Create` が接続の外に起動します。非ログインシェルでも `uv` などを見つけられるように、各段階で `~/.local/bin` を PATH に加えます。各段階の結果は Windows の一時ディレクトリに残ります。SSH を切ってから読み直すときは、同じ Windows ユーザーを使います。

Define this in Windows PowerShell. `Win32_Process.Create` launches outside the Windows OpenSSH session. Each stage adds `~/.local/bin` to PATH so the non-login shell can find tools such as `uv`, and leaves a log in the Windows temp directory. Reconnect as the same Windows user to read it.

```powershell
$Distro = 'Ubuntu'
$Logs = Join-Path $env:TEMP 'wsl-anchor-acceptance'
New-Item -ItemType Directory -Path $Logs -Force | Out-Null

function Start-WslCheck([string]$Stage, [string]$Bash) {
    $Log = Join-Path $Logs "$Stage.log"
    $Script = 'export PATH="$HOME/.local/bin:$PATH"' + "`n" + $Bash.Replace("`r", '')
    $Payload = @{
        distro = $Distro
        log = $Log
        script = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Script))
    } | ConvertTo-Json -Compress
    $Payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Payload))
    $Command = @'
$Config = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json
& "$env:SystemRoot\System32\wsl.exe" -d $Config.distro --exec bash -c ("set -o pipefail; printf %s " + $Config.script + " | base64 -d | bash") *> $Config.log
Add-Content -LiteralPath $Config.log -Value "wsl exit=$LASTEXITCODE"
'@
    $Encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command.Replace('__PAYLOAD__', $Payload)))
    $Exe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $Result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = ('"{0}" -NoProfile -NonInteractive -EncodedCommand {1}' -f $Exe, $Encoded)
    }
    if ($Result.ReturnValue -ne 0) { throw "Win32_Process.Create returned $($Result.ReturnValue)" }
    "Windows launcher PID $($Result.ProcessId). Log $Log"
}
```

## 1. 導入と起動し直し / Install and cold start

linger が無効な場合は、先に試験用アカウントのログインシェルで `systemctl --user show-environment` が成功し、`loginctl show-user "$USER" --property=Linger --value` が `no` を返すことを確認します。そのまま installer を実行し、`dashboard/agentctl.sh status` の `service mode: supervised-background` と `/api/version` の応答を確認します。次に下の手順で linger を有効にし、同じ導入先を更新します。

If linger is disabled, first confirm from the test account's login shell that `systemctl --user show-environment` succeeds and `loginctl show-user "$USER" --property=Linger --value` returns `no`. Run the installer in that shell and check for `service mode: supervised-background` in `dashboard/agentctl.sh status` and a response from `/api/version`. Then enable linger and update the same installation below.

試験用 checkout で #115 の修正を導入します。Mail の修正も実機で見る場合は、別ブランチの commit を試験用 checkout に取り込んでから導入します。以下では既存 Mail を再利用します。追加の導入操作が必要なときはその確認を行える端末で済ませます。既存 dashboard が supervised background なら、その PID が消えて user unit が `enabled` / `active` になることを確認します。

Install the #115 fix from the test checkout. To exercise the Mail fix as well, apply its separate commit to the test checkout before installing. This example reuses an existing Mail install. Complete any interactive setup in a terminal first. When replacing a supervised-background dashboard, verify that its PID disappears and the user unit becomes `enabled` and `active`.

```powershell
Start-WslCheck install @'
set -eu
cd "$HOME/work/orrery-telemetry"
loginctl enable-linger "$USER"
scripts/install.sh --keep-mail --wsl-anchor
systemctl --user is-enabled org.agentstack.agentdashboard.service org.agentstack.mail.timer
systemctl --user is-active org.agentstack.agentdashboard.service org.agentstack.mail.timer
'@
```

`install.log` に `wsl exit=0` が出たら、作業が残っていないことを確認して `wsl --shutdown`。PowerShell から次を起動します。dashboard の起動、約1分後の Mail の起動、pidfile の診断を記録します。PID が再利用されなかった回は、その回で Mail の古い pidfile の分岐を実機確認できたとは数えません。

After `install.log` shows `wsl exit=0`, confirm all WSL work is saved and run `wsl --shutdown`. Launch the following from PowerShell. It records dashboard startup, Mail startup after about a minute, and pidfile diagnostics. Count coverage of the stale Mail pidfile recovery only when the old PID was actually reused.

```powershell
Start-WslCheck cold-start @'
set -eu
dashboard=org.agentstack.agentdashboard.service
mail=org.agentstack.mail.service
for ((i=0; i<30; i++)); do
    if systemctl --user is-active --quiet "$dashboard"; then break; fi
    sleep 1
done
systemctl --user is-active "$dashboard"
curl -fsS http://127.0.0.1:8770/api/version
sleep 75
journalctl --user -b -u "$dashboard" -u "$mail" --no-pager
"$HOME/.agentstack/bin/agentstack-mailctl" status
'@
```

## 2. PID と保持 / PID and hold

他の Ubuntu ウィンドウを閉じてから、設定済みの Codex を試験専用 tmux セッションに起動します。下の記録で `state=holding`、`anchor_running=true`、正の整数の `windows_pid` を確認します。Windows から PID とコマンドラインを照合し、元の Windows 起動役が終わって SSH を切った後も、その `wsl.exe` と distro が残ることを確認します。

Close other Ubuntu windows and start the configured Codex CLI in a dedicated tmux session. The log should show `state=holding`, `anchor_running=true`, and a positive integer `windows_pid`. Match the PID and command line on Windows. Verify that the client and distro remain up after the launcher exits and SSH is disconnected.

```powershell
Start-WslCheck hold @'
set -eu
. "$HOME/.agentstack/env.sh"
tmux new-session -d -s wsl-anchor-acceptance 'exec "$AGENTSTACK_CODEX_BIN"'
ok=0
for ((i=0; i<90; i++)); do
    if curl -fsS http://127.0.0.1:8770/api/wsl-anchor | python3 -c '
import json, sys
status = json.load(sys.stdin)
assert status["state"] == "holding" and status["anchor_running"]
assert type(status["windows_pid"]) is int and status["windows_pid"] > 0
print(status)
'; then ok=1; break; fi
    sleep 1
done
test "$ok" = 1
"$HOME/.agentstack/bin/agentstack-doctor"
'@

# Run from Windows after reading windows_pid in hold.log.
Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" |
    Select-Object ProcessId, ParentProcessId, CommandLine
wsl.exe --list --verbose
```

### Windows OpenSSH から直接起動する確認 / Direct Windows OpenSSH acceptance

手順2の WMI 起動とは別の回で、既存の保持を解放し、他の Ubuntu の窓を閉じます。Windows OpenSSH に接続し、接続内から `wsl.exe -d Ubuntu` で Ubuntu に入ります。`/api/wsl-anchor` が `idle` を返すことを確認し、手順2の Bash 本文をそのシェルで実行して保持用 PID を記録します。Windows の別の観測経路で、その PID の `wsl.exe` と対象 distro を確認してから、元の SSH 接続を切ります。45秒以上経過した後、Windows 側だけから保持用 PID と distro の `Running` を再確認します。その確認が済んでから下の短いコマンドを WMI で起動し、試験用 tmux と同じ保持用 PID を確認します。手順3で同じ保持を解放します。

Run this separately from the WMI launch in step 2. Release any previous hold and close other Ubuntu windows. Connect through Windows OpenSSH and enter Ubuntu with `wsl.exe -d Ubuntu` from that connection. Confirm that `/api/wsl-anchor` returns `idle`, then run the Bash body of step 2 in that shell and record the hold PID. From a separate Windows observation channel, confirm the hold process and distro, then disconnect the original SSH session. After at least 45 seconds, confirm the same Windows PID and `Running` state from Windows alone. Then use the short WMI-launched probe below to confirm the test tmux session and the same hold PID. Release the same hold in step 3.

```powershell
Start-WslCheck ssh-survived @'
set -eu
tmux has-session -t =wsl-anchor-acceptance
curl -fsS http://127.0.0.1:8770/api/wsl-anchor | python3 -c '
import json, sys
status = json.load(sys.stdin)
assert status["state"] == "holding" and status["reasons"]["agents"] > 0
print(status["windows_pid"])
'
'@
```

## 3. ORRERY の保持解除 / ORRERY hold release

下の段階で試験用セッションだけを終了します。ログで `releasing` を経て、約60秒の無活動後に `state=idle`、`anchor_running=false`、`windows_pid=null` がそろうことを確認します。手順2で記録した Windows の保持用 PID が消えれば、ORRERY の保持解除は完了です。

End only the test session. Check the log for `releasing`, followed after about 60 seconds of inactivity by `state=idle`, `anchor_running=false`, and `windows_pid=null` together. Verify that the Windows hold PID recorded in step 2 exits to complete the ORRERY hold-release check.

```powershell
Start-WslCheck release @'
set -eu
tmux kill-session -t =wsl-anchor-acceptance
for ((i=0; i<80; i++)); do
    if curl -fsS http://127.0.0.1:8770/api/wsl-anchor | python3 -c '
import json, sys
status = json.load(sys.stdin)
print(status)
released = (
    status["state"] == "idle"
    and status["anchor_running"] is False
    and status["windows_pid"] is None
)
sys.exit(0 if released else 1)
'; then exit 0; fi
    sleep 1
done
exit 1
'@
```

## 4. WSL の自然停止と対照 / WSL idle shutdown and control

手順3の Windows 起動役が終了した後、Windows からだけ distro の状態と `wsl.exe`・`wslhost.exe` の一覧を記録します。60秒後と120秒後にも採取します。Linux 側へ入ると観測自体が WSL クライアントになります。ORRERY の保持を解除しても、WSL が別のクライアントを追跡していれば distro は `Running` のままです。

After the Windows release launcher exits, record the distro state and the `wsl.exe` and `wslhost.exe` processes from Windows only. Take further snapshots after 60 and 120 seconds. Entering Linux would add another WSL client. The distro can remain `Running` after ORRERY releases its hold while WSL tracks other clients.

```powershell
function Show-WslCheckState {
    Get-Date -Format o
    Get-CimInstance Win32_Process -Filter "Name='wsl.exe' OR Name='wslhost.exe'" |
        Select-Object ProcessId, ParentProcessId, CreationDate, CommandLine
    wsl.exe --list --verbose
}
Show-WslCheckState
```

対照は別の cold start で採ります。ORRERY や agent が動かない試験用 distro を選び、systemd や idle timeout は本試験と同じ設定にします。同じ distro で導入前に採っても構いません。WMI から `/bin/true` だけを起動し、その `wsl.exe` が終了した後に同じ間隔で `Show-WslCheckState` を実行します。distro の停止と `wslhost.exe` の残り方を手順3の観測と比べ、ORRERY の保持解除の結果とは別に記録します。

Take a separate cold-start control in a test distro without ORRERY or agents running, using the same systemd and idle-timeout settings. You can run it in the same distro before installation. Launch only `/bin/true` via WMI. After its `wsl.exe` exits, run `Show-WslCheckState` at the same intervals. Compare distro shutdown and any remaining `wslhost.exe` processes with step 3, and record the comparison separately from the ORRERY hold-release result.

```powershell
$ControlDistro = 'Ubuntu'
$Wsl = "$env:SystemRoot\System32\wsl.exe"
$Control = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = ('"{0}" -d "{1}" --exec /bin/true' -f $Wsl, $ControlDistro)
}
if ($Control.ReturnValue -ne 0) { throw "Win32_Process.Create returned $($Control.ReturnValue)" }
"Control wsl.exe PID $($Control.ProcessId)"
```

試験後は `.wslconfig` の一時変更を戻します。Restore any temporary `.wslconfig` changes after the test.
