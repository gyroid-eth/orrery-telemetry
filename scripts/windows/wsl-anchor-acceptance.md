# WSL 自動保持・再起動の実機確認 / WSL hold and restart acceptance

Windows 11 / WSL2 の試験用 Ubuntu を使います。systemd と Windows interop を有効にし、従来モードで Mail と dashboard を導入します。再現時と同じ条件にするなら、作業を保存してから `.wslconfig` の負の idle timeout を試験中だけ外し、次に `wsl --shutdown` で設定を反映します。dashboard のページは閉じておきます。以下の port、unit 名、リポジトリ位置は試験環境に合わせます。

Use a test Ubuntu distro on Windows 11 / WSL2 with systemd and Windows interop enabled. Use a legacy-mode install of Mail and the dashboard. To match the original reproduction, save all WSL work, temporarily remove any negative idle timeouts from `.wslconfig` and apply the change with `wsl --shutdown`. Close dashboard pages. Adjust the port, unit names and checkout location for the test installation.

## Windows 側の起動関数 / Windows launcher

Windows 上の PowerShell で定義します。Windows OpenSSH 経由でも `Win32_Process.Create` が接続の外に起動します。各段階の結果は Windows の一時ディレクトリに残ります。SSH を切ってから読み直すときは、同じ Windows ユーザーを使います。

Define this in Windows PowerShell. `Win32_Process.Create` launches outside the Windows OpenSSH session, and each stage leaves a log in the Windows temp directory. Reconnect as the same Windows user to read it.

```powershell
$Distro = 'Ubuntu'
$Logs = Join-Path $env:TEMP 'wsl-anchor-acceptance'
New-Item -ItemType Directory -Path $Logs -Force | Out-Null

function Start-WslCheck([string]$Stage, [string]$Bash) {
    $Log = Join-Path $Logs "$Stage.log"
    $Payload = @{
        distro = $Distro
        log = $Log
        script = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Bash.Replace("`r", '')))
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

試験用 checkout で #115 の修正を導入します。Mail の修正も実機で見る場合は、別ブランチの commit を試験用 checkout に取り込んでから導入します。以下では既存 Mail を再利用します。追加の導入操作が必要なときはその確認を行える端末で済ませます。既存 dashboard が supervised background なら、その PID が消えて user unit が `enabled` / `active` になることを確認します。

Install the #115 fix from the test checkout. To exercise the Mail fix as well, apply its separate commit to the test checkout before installing. This example reuses an existing Mail install. Complete any interactive setup in a terminal first. When replacing a supervised-background dashboard, verify that its PID disappears and the user unit becomes `enabled` and `active`.

```powershell
Start-WslCheck install @'
set -eu
cd "$HOME/work/orrery-telemetry"
scripts/install.sh --keep-mail --wsl-anchor
loginctl enable-linger "$USER"
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

## 3. 解除と停止 / Release and stop

下の段階で試験用セッションだけを終了します。dashboard が `releasing` に変わり、約60秒の無活動で `idle` に移ることをログで確認します。Windows 側でも手順2で記録した PID が消え、他のクライアントが無い場合は distro が `Stopped` になることを確認します。観測中は Windows 側のプロセス一覧と `wsl --list --verbose` を使います。distro に入ると別の WSL クライアントになります。

End only the test session. Check the log for `releasing` followed by `idle` after about 60 seconds of inactivity. Verify on Windows that the PID recorded in step 2 exits and that the distro becomes `Stopped` if there are no other clients. Observe using the Windows process list and `wsl --list --verbose`. Opening a new shell in the distro adds a WSL client.

```powershell
Start-WslCheck release @'
set -eu
tmux kill-session -t =wsl-anchor-acceptance
for ((i=0; i<80; i++)); do
    if curl -fsS http://127.0.0.1:8770/api/wsl-anchor | python3 -c '
import json, sys
status = json.load(sys.stdin)
print(status)
sys.exit(status["state"] != "idle")
'; then exit 0; fi
    sleep 1
done
exit 1
'@
```

保持用 PID の消滅後も distro が残る場合は、Windows の `wsl.exe` と `wslhost.exe` の PID とコマンドラインを記録します。元に戻す場合は `.wslconfig` の試験変更を戻します。

If the distro remains running after the hold PID exits, record the PIDs and command lines of `wsl.exe` and `wslhost.exe` on Windows. Restore any temporary `.wslconfig` changes after the test.
