Community-maintained / experimental. Validation environment: Windows 11 build 26200, CPython 3.12.10, Windows PowerShell 5.1, Codex 0.153.4, and WinGet `arndawg.tmux-windows` version `3.6a-win32.7` (binary `tmux3.6a-win32`).

# Windows Codex child launcher (PR1)

PR1 for [Issue #14](https://github.com/gyroid-eth/orrery-telemetry/issues/14) is an experimental boundary for starting a pre-registered Codex child in its own native-Windows tmux server.
Native Windows is not a supported ORRERY Telemetry environment.
WSL2 is the Windows path maintained for verification.
Dashboard NEW AGENT / SPAWN stays disabled, and this launcher does not enable Dashboard SPAWN.

This launcher does not register an identity, establish contact, or deliver a task.
The parent agent uses ORRERY Mail to pre-register the child identity, prepare contact and the canonical task, and then runs the command below.

## Before launch

- Install native tmux separately.
  The validation environment used `arndawg.tmux-windows` `3.6a-win32.7` and the `tmux3.6a-win32` binary.
- `--codex-home` must already be an existing child-specific private directory.
  Its ACL must allow access only to the current user SID.
  Do not put `config.toml` in this directory; the launcher writes the child-specific file.
- Prepare Codex authentication, login, and first-run trust / permission setup in advance.
  The launcher does not create credentials or complete first-run setup.
  Its `[windows] sandbox = "elevated"` entry is a child launch setting; it does not complete Codex or Windows authentication and setup.
  If trust, sign-in, setup, or approval screens remain, the readiness timeout stops the launch and no task is injected.
- When the parent pre-registers the child in Mail, put the resulting owner token in a one-line child token handoff file.
  The handoff file and the `--mail-env` env file must also have a current-user-only private ACL.

## Launch

Pass existing absolute `.exe` paths explicitly to `--codex` and `--python`.
`--mail-url` is the Mail endpoint, and `--mail-env` is the path to its transport configuration file.
Do not put the owner-token value or a bearer token in the command line, prompt, or inline environment.

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

The main arguments are:

| Argument | Behavior |
|---|---|
| `--name`, `--parent` | The child and parent names already prepared in Mail. PR1 does not accept a parentless launch. |
| `--cwd` | Existing directory where the child works. |
| `--project` | Project key passed to Mail. |
| `--codex-home` | ACL-verified child-specific `CODEX_HOME`; an existing `config.toml` is rejected. |
| `--state-directory` | Private root where the launcher creates per-launch state; its parent must already exist. |
| `--child-token-file` | One-line token handoff file with a current-user-only ACL; the source is consumed on success. |
| `--mail-url` | Mail endpoint. The example shows a loopback MCP URL. |
| `--mail-env` | Absolute path to the Mail env file; when omitted, `AGENTSTACK_MAIL_ENV` is used. |
| `--bearer-mode` | `enabled` (the default) or `disabled`. Disabled removes any ambient bearer token from the proxy environment. |
| `--codex`, `--python` | Existing absolute `.exe` paths; both are explicit in the example. |
| `--model` | Codex model passed to the child. |
| `--effort` | `low`, `medium`, `high`, `xhigh`, or `max`; default `xhigh`. |
| `--approval` | `never`, `on-request`, or `untrusted`; default `never`. |
| `--ready-timeout` | Seconds to wait for a recognized prompt; default 90 seconds. |

The launcher starts its own tmux server and records that server's PID, creation time, and executable before issuing a client command.
If the server exits before its named pipe is ready, the launcher fails without creating a second server.
If the child never reaches a recognized prompt, the pane is saved in private state and no task is sent.
When trust or first-run setup needs a human action, attach with the `tmux_socket` and child name shown at launch:

```powershell
tmux -S '<tmux_socket>' attach-session -t '<session>'
```

Use the exact `tmux_socket` and `session` values from the launch result / stderr.
Use `-S`, not `-L`, for the socket.

## Token and state boundaries

`--child-token-file` must have a private ACL, be a regular file of at most 4096 bytes, and contain one non-empty token line.
After verification, the launcher exclusively copies the token value into the per-launch state's `owner.token` and removes the source handoff file.
The token value is absent from Codex argv and the task prompt; the child proxy receives only the private `owner.token` path.

`owner.token` is removed by `stop` or when the child exits naturally.
That cleanup does not retire the Mail identity.
The identity, contact, and task history prepared by the parent remain the parent's responsibility, and a failed launch result reports that registration was retained.

The Codex child process is assigned to a kill-on-close Windows job.
If Codex exits first while an MCP process or another descendant remains, closing the job terminates that descendant.
A process outside the job is left running.

`run_codex_proxy.py` reads the private `AGENTSTACK_MAIL_ENV` file line by line.
It never evaluates the file as a shell script, so env-file contents are not executed as commands.
With `--bearer-mode enabled`, `HTTP_BEARER_TOKEN` is required; `disabled` removes any ambient `MCP_AGENT_MAIL_TOKEN`.

To stop a launch, use the exact per-launch path returned as `state_directory` in the launch result.
Do not infer the parent state root.

```powershell
$State = 'C:\path\to\private-launch-state\<state-directory-returned-by-launch>'
& $Python -X utf8 $Launcher stop --state-directory $State
```

The stop operation acts only on processes whose recorded PID, creation time, and executable still match, plus descendants of those owned processes.
It leaves unrelated user processes alone and does not recursively delete a user sandbox or a junction target.
Paths containing reparse points are rejected as private state.
If a PID has been reused or its creation time / executable no longer matches, it is not terminated.

## Readiness and validation scope

Readiness recognizes an idle Codex prompt with a displayed model.
Startup, sign-in, trust, setup, approval, and usage-limit screens are blocked states.
Screens requiring human action are never confirmed automatically; a timeout fails closed.

The focused regression run was 15 tests passed in `tests/windows/test_codex_launcher.py`.
It exercises native-Windows ACLs, token handoff, proxy bearer environment handling, owned-process cleanup, the kill-on-close job, PID reuse protection, readiness, and runtime environment propagation.
This result is recorded separately from the real smoke run.
It does not establish full installer, Codex App Bridge, Dashboard SPAWN, Mail-history, or native-Windows-wide success.
