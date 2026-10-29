# Switch to an empty Mail instance (in preparation)

[日本語](fresh-global-update.md) · [User-level instructions](instruction-migration.en.md)

**This guide cannot yet complete a production cutover.** Empty Mail and explicit clients, automatic children, managed harnesses, App adapters, screen APIs, PC input, R4 and ended-agent cleanup have limited fixture coverage. Private checks also cover saved settings and Mail/controller → dashboard host → Cockpit startup, refusals and owned-process shutdown. Real OS managers/Finder GUI/App/provider/login/trust/live-install acceptance, final CI and an attended cutover remain incomplete. Japanese paths on case-insensitive Macs are supported **only within the restricted profile on natively identified APFS**; characters outside it and other filesystems such as HFS still refuse with `CASE_RULES_UNKNOWN`. Windows-side WSL folders have not passed acceptance on a real machine. Check the scope below for your working paths and stop if they fall outside it. Do not run the following against an existing installation until the outstanding work is accepted and the operator has approved an attended cutover.

One machine has one human user, whose agents trust each other. The old Mail stores are not merged: the new Mail instance starts with an empty database. Even a reused name or numeric ID represents a new registration. Old unread state, credentials, reservations, delivery state, window bindings, and saved resume connections are not inherited. Selected message text and attachment references can be carried over manually from the archive CLI output.

## Give this task to your agent

Run the fresh dashboard and Cockpit on the same host and UID with the same verified `AGENTSTACK_FRESH_CLIENT_CONTEXT`. Start the dashboard first, and restart Cockpit after a dashboard restart. Stop the dashboard before replacing or restoring the DB, then restart it after the work. Stop if the context or private host connection is unavailable or revoked.

| Scope | Current coverage and remaining work |
|---|---|
| Automatic children, New Agent, managed/Local | Limited checks cover the same I with distinct A/contexts through existing entries and Mail to the specified parent. Real endpoints and human login/trust need separate acceptance |
| Mail/graph, PC Terminal, R4, ended cleanup | Limited checks cover finite readers, selected-pane input/output, same-native resume, confirmed input and same-pane cleanup after refusing attached clients. They do not guarantee the whole real UI or real providers |
| App | Synthetic fixtures cover notifications, waiting-root wake and external-child adapters. Real Desktop/human trust remain unaccepted |
| Distribution and ordinary startup | Private installed-launcher and saved-setting Mail/dashboard/Cockpit startup/shutdown checks passed using the development Mail module. Wheel/sdist, real OS managers/Finder GUI and live installation remain unaccepted |
| Reactivation and App foregrounding | Reactivate, retired resume and App GUI foregrounding are outside this cutover scope and remain unconnected. They are distinct from ordinary jump to a running target, App notifications and external children |

Neither API34 nor a documentation update is a cutover signal.

> Read docs/fresh-global-update.en.md and prepare a list of work to continue and a recovery procedure. Report outstanding acceptance work without executing a cutover. Once the version is ready, perform the agent steps and stop at each operator step to explain the required action. Do not read credentials or storage.sqlite3 directly; use only the archive CLI to read the archive.

| Step | Agent work | Operator step | Verification |
|---|---|---|---|
| 1 | Record continuing work, unread messages, pending replies, file owners, the old version, and start/stop/recovery procedures | Choose what to carry over | A handoff note without credentials and a recovery procedure exist |
| 2 | Identify every writer and restart source; prepare shutdown | Approve shutdown while present; take over the maintenance terminal if the agent must stop too | Work agents, Mail, proxies, watchers, GC, and restart sources have stopped |
| 3 | Preserve the stopped installation and distribute the approved version | Confirm the archive destination and update | The old installation is preserved unchanged; the recovery version/settings are available |
| 4 | Initialize an explicit separate root, check health, and serve in the foreground | Confirm the endpoint and startup action | A different Mail instance and empty registration namespace, separate from old connections |
| 5 | Register again, prepare the client, and check provider preparation | Login, instruction changes requiring consent, and Codex hook trust approval | Current reconnect identifies the same new owner; incomplete preparation stops launch |
| 6 | Find selected old content through the archive CLI; send new Mail or reserve a note before editing it | Confirm selected content | New messages have new IDs/timestamps; the archive state is unchanged |
| 7 | Acquire new reservations; check messages, notifications, and edits across folders | Decide whether to resume work | Conflicting access to the same file is refused; owner, recipient, and target paths match |
| 8 | On failure, stop and preserve the new side too; recover the old operation by procedure | Decide whether to recover | Old and new sides do not run together; unfinished new work is recorded |

## 1–3. Preserve a way back before stopping

### Explicit dashboard and Cockpit preparation (ordinary service cutover is incomplete)

Select an existing fresh client as the preparation root and reconnect to check I/A and root. Prepare its provider HOME through existing setup and human Codex hook approval. A parented launch also requires the specified parent's protected client and provider preparation. Never find credentials or contexts from browser JSON. standalone=true adds no report recipient for the preparation root; an explicit parent needs a unique name or I/A.

In a separate maintenance terminal, reset the verified `orrery_python`, `orrery_install`, `orrery_context` and `orrery_work_root`; the last is the allowed working folder. `orrery_socket` is a private tmux server socket prepared by the operator/agent through existing tmux procedures, with verified ownership, 0700 parent directory and canonical path (stop if too long). Never substitute a default/live socket. `orrery_dashboard_port` is a verified unused non-live port. Stop if any value is unresolved.

```sh
env AGENTSTACK_PYTHON="$orrery_python" AGENTSTACK_HOME="$orrery_install" \
  AGENTSTACK_RUNTIME_MODE=fresh AGENTSTACK_CLIENT_CONFIG= AGENTSTACK_GLOBAL_SERVER_CONFIG= \
  AGENTSTACK_FRESH_CLIENT_CONTEXT="$orrery_context" \
  AGENTSTACK_FRESH_DASHBOARD_SOCKET="$orrery_socket" \
  AGENTSTACK_SPAWN_ROOTS="$orrery_work_root" AGENTSTACK_SPAWN_DIRS="$orrery_work_root" \
  AGENTSTACK_BIND_HOST=127.0.0.1 AGENTSTACK_PORT="$orrery_dashboard_port" \
  "$orrery_python" "$orrery_install/dashboard/server.py"
```

Pass previously verified absolute provider binary and managed manifest paths through the existing environment settings in that terminal. Do not switch clients during the process lifetime. No daemon or automatic service cutover is added; stopping this foreground process stops the dashboard.

Check runtime_mode/I/capabilities at /api/version, I/A at /api/agents, one standalone and one parented New Agent launch, then native_ready/end records at /api/spawn-status?name=... . Pending alone does not prove startup. For uncertain outcomes or errors after preparation, retain job/I/A and inspect the existing process and preparation records through the procedure; do not automatically create the same child again. Unprepared providers or revoked credentials stop explicitly.

Worktree, browser, custom base/tools, opening a terminal, role/group/emoji annotations and dry_run are explicitly unavailable at this stage. The agent can prepare needed folders/provider settings with existing procedures. Ordinary Claude/Codex/dsh/Hermes/Local standalone/parented automatic launches are not replaced with manual child creation. A roster native receipt alone does not establish current OS liveness. See the [API contract](api.en.md) for Mail/graph/Terminal routes, bounds and refusals.

After the dashboard responds, restore the same variables in a separate maintenance terminal and start Cockpit. `orrery_instance` is the new I verified by health; `orrery_cockpit_root` is the matching checkout; `orrery_cockpit_python` is an approved interpreter with its public dependencies; `orrery_cockpit_port` is a verified free port; `orrery_tmux` is the verified absolute binary. Stop if any value is unresolved.

```sh
orrery_instance='<mail_instance_id-verified-by-health>'
orrery_cockpit_root='/absolute/path/to/matching/cockpit'
orrery_cockpit_python='/absolute/path/to/approved/cockpit-python'
orrery_cockpit_port='<confirmed-free-cockpit-port>'
orrery_tmux='/absolute/path/to/tmux'
env AGENTSTACK_RUNTIME_MODE=fresh AGENTSTACK_CLIENT_CONFIG= AGENTSTACK_GLOBAL_SERVER_CONFIG= \
  AGENTSTACK_HOME="$orrery_install" AGENTSTACK_PYTHON="$orrery_python" \
  AGENTSTACK_FRESH_CLIENT_CONTEXT="$orrery_context" \
  ORRERY_FRESH_MAIL_INSTANCE_ID="$orrery_instance" \
  ORRERY_DASHBOARD_URL="http://127.0.0.1:$orrery_dashboard_port" \
  "$orrery_cockpit_python" "$orrery_cockpit_root/bridge/orrery_backend.py" \
    --host 127.0.0.1 --port "$orrery_cockpit_port" --tmux-bin "$orrery_tmux"
```

This starts an explicit backend. With prepared context/socket references explicitly selected, the installer saves the same config/I and passes them to normal services, preserving them on updates. The selected Python must already provide the matching Mail module. Cockpit script and Finder share a literal settings loader and check the same I/context digest/epoch. Set telemetry_home in Cockpit shared config for a custom installation; Finder does not inherit shell exports. Do not start duplicate backends. R4 needs the separate human opt-in in the [mobile guide](mobile.en.md#inspect-a-pane-and-send-input-from-your-phone).

Determine the actual versions, explicit configurations/roots, service managers, and exact stop/restart commands from the installation's [install guide](install.md), [configuration](configuration.md), and registered settings. This page does not guess launchd/systemd names or shutdown commands. Stop if the endpoint or manager is unknown; do not substitute another database or port.

Record work, pending replies, and file ownership first. Before editing a note, reserve its path through the currently valid coordination connection. Do not keep editing after a reservation expires or communication fails. Old reservations do not authorize work after shutdown.

After stopping all writers and automatic restart sources, preserve the entire old installation separately: the database, WAL/related sidecars, archives/attachments, settings, and launch records. Treat the preserved archive as read-only for agents. Do not restart the old Mail server or watcher for browsing. Old `fetch_inbox` consumes signals and is not an archive reader.

The archive CLI accepts only an explicitly selected, **consistent and finalized database copy**. Do not copy only the database without confirming shutdown, or delete/rename a nonempty WAL to make a copy appear finalized. The CLI refuses nonempty `-wal`/`-journal` files; that check alone does not prove the copy is consistent. There is currently no product command to finalize the copy. Stop the archive-reading step if a consistent finalized copy cannot be provided. Agents have no exception allowing direct `sqlite3`, custom SQL reads, or checkpoints.

Prepared fresh references are connected to normal startup paths, while actual service cutover/recovery and acceptance in the operator environment remain outstanding. An ordinary update does not automatically switch to the new side. Do not proceed in production until the approved distribution procedure exists.

## 4. Initialize an explicit empty root

These are command forms for a maintenance terminal. Replace `<...>` and `/absolute/...` with verified values before executing them. `orrery_python` is the approved interpreter that can actually import this version of the Mail package; `orrery_install` is the matching distribution containing the launchers/hooks. Do not add dependencies to a service virtual environment yourself.

```sh
orrery_python='/absolute/path/to/approved/python'
orrery_install='/absolute/path/to/matching/orrery-install'
orrery_fresh_root='/absolute/canonical/path/to/new-mail-root'
orrery_mail_port='<confirmed-free-mail-port>'
orrery_config="$orrery_fresh_root/config.json"
```

The parent directory must exist. Use an absolute canonical root without symlink components, separate from the old Mail installation. **Do not mkdir the root first: initialization refuses every existing destination, including an empty directory.** If initialization fails and leaves a destination, preserve and inspect it rather than deleting it to retry.

```sh
"$orrery_python" -m agentstack_mail.fresh_cli init \
  --mode fresh --root "$orrery_fresh_root"
"$orrery_python" -m agentstack_mail.fresh_cli health \
  --config "$orrery_config"
```

Check `initialized`, `mode: fresh`, and that health's `mail_instance_id` matches the initialization result. Do not write the old instance ID into the new configuration. Health's `activation_enabled: false` means dormant global mode has not been activated; this procedure does not change that flag. Omitting `--mode` selects the temporary-root isolated mode.

In a separate maintenance terminal, use the verified free port to serve in the foreground. **Every time you open another terminal, repeat the initial variable definitions with the same verified values: check `orrery_python`, `orrery_install`, `orrery_fresh_root`, `orrery_mail_port`, and the resulting `orrery_config`.** Variables from the original shell are not automatically inherited by an independently opened terminal, even if exported. This command does not register an OS service.

```sh
"$orrery_python" -m agentstack_mail.fresh_cli serve \
  --config "$orrery_config" --port "$orrery_mail_port"
```

The endpoint is `http://127.0.0.1:<same-port>/mcp`. Only explicit fresh mode permits 8765; that is not permission to use an occupied port without stopping its service. Ports 8770/8781/8791 are forbidden for Mail. Development evidence so far uses temporary roots and owned dynamic ports only; production 8765 binding and real root creation remain unaccepted.

## 5. Register again and prepare a protected client

Register each agent from a trusted local maintenance terminal. Generate and retain a registration UUID before the first request. If the response is lost, repeat **the same UUID and the same name/program/model**, rather than creating another registration. Credentials stay in a private binding; the result returns IDs and the binding path. Do not paste binding/registration-intent contents into model tool arguments or handoff notes.

```sh
orrery_registration_id='<saved-registration-UUID>'
orrery_agent_name='<chosen-agent-name>'
orrery_program='<actual-program>'
orrery_model='<actual-model>'
"$orrery_python" -m agentstack_mail.fresh_cli register \
  --config "$orrery_config" --request-id "$orrery_registration_id" \
  --name "$orrery_agent_name" --program "$orrery_program" --model "$orrery_model"
```

Use the returned `binding_path` unchanged. Generate and retain a separate client UUID before preparing it. Prepare does not register another owner.

```sh
orrery_binding='/absolute/path/returned/as/binding_path'
orrery_client_id='<saved-client-UUID>'
orrery_endpoint="http://127.0.0.1:$orrery_mail_port/mcp"
"$orrery_python" -m agentstack_mail.fresh_client prepare \
  --config "$orrery_config" --binding "$orrery_binding" \
  --endpoint "$orrery_endpoint" --client-id "$orrery_client_id"
```

Use the returned `context_path` to verify current access to the same instance/agent.

```sh
orrery_context='/absolute/path/returned/as/context_path'
printf '%s\n' '{}' | "$orrery_python" -m agentstack_mail.fresh_client call-json \
  --context "$orrery_context" --tool reconnect
```

### Install user-level fresh instructions with the existing setup CLI

The target is `providers/claude` or `providers/codex` beside the returned `context_path`. This version's normal core payload ships `templates/fresh`, the existing hook preparation CLIs under `scripts/`, and Codex plugin source/schemas (excluding dashboard-only installs). First check that the distribution contains `templates/fresh/claude/CLAUDE.md` and `templates/fresh/codex/AGENTS.md`. Stop if files are missing rather than substitute legacy templates. Shipping the payload neither switches the service nor changes the default mode; final guide indexes and attended cutover remain separate. The example below uses Claude; set `orrery_provider` to `codex` to prepare **instructions only** for Codex. In another maintenance terminal, repeat the previously verified variable definitions and `orrery_context` too.

```sh
orrery_provider='claude'
orrery_client_dir="${orrery_context%/context.json}"
orrery_provider_home="$orrery_client_dir/providers/$orrery_provider"
orrery_setup="$orrery_install/bin/agentstack-$orrery_provider-setup"
```

Use the exact `context.json` path returned by prepare. For a new setup only, create the `providers` directory and the selected provider directory with mode0700. For existing directories, verify ownership, mode0700, and absence of symlinks rather than recreate them. Do not touch the instruction file first or bulk-copy credentials, settings, or bindings from an old HOME.

Run the existing setup's read-only plan with the fixed provider HOME as cwd. Both provider HOME variables point to the same explicit directory only for this setup process. It creates the selected provider's user-level file; it does not select project-level instructions.

```sh
(
  cd "$orrery_provider_home" || exit
  env AGENTSTACK_PYTHON="$orrery_python" AGENTSTACK_HOME="$orrery_install" \
    AGENTSTACK_TEMPLATE_HOME="$orrery_install/templates/fresh" \
    CLAUDE_CONFIG_DIR="$orrery_provider_home" CODEX_HOME="$orrery_provider_home" \
    CLAUDE_HOME= AGENTSTACK_CLAUDE_MD_SCOPE=global \
    AGENTSTACK_PROJECT_KEY= AGENTSTACK_LEGACY_PROJECT_KEY= \
    "$orrery_setup" --plan --json
)
```

Check `target_path`/`resolved_home`, the selected fresh template, affected files, and `plan_id`. Planning creates no files or locks. Follow the [instruction migration consent contract](instruction-migration.en.md#consent): **hand existing-content changes to the operator's terminal for confirmation**. Creating only a new file needs no additional consent under the existing engine's contract. An agent must not add `--assume-yes` or bypass a pending confirmation through another writer.

Apply the verified plan ID with **the same cwd and every environment selection**. Do not paste only the returned `apply_argv` into another terminal without its environment.

```sh
orrery_instruction_plan='<verified-plan_id>'
(
  cd "$orrery_provider_home" || exit
  env AGENTSTACK_PYTHON="$orrery_python" AGENTSTACK_HOME="$orrery_install" \
    AGENTSTACK_TEMPLATE_HOME="$orrery_install/templates/fresh" \
    CLAUDE_CONFIG_DIR="$orrery_provider_home" CODEX_HOME="$orrery_provider_home" \
    CLAUDE_HOME= AGENTSTACK_CLAUDE_MD_SCOPE=global \
    AGENTSTACK_PROJECT_KEY= AGENTSTACK_LEGACY_PROJECT_KEY= \
    "$orrery_setup" --apply --plan-id "$orrery_instruction_plan" --json
)
```

Check `stage: complete` and the target file. For existing-file changes, the shared engine retains private full-file backups/receipts and preserves bytes outside the markers. Stop on changed inputs, pending receipts, or missing consent, following the reported reason. Generating instructions neither registers new credentials nor approves Codex hooks.

This does not add `instruction-configure` to the fresh wrapper or reuse the old global configure route. Normal launch only checks the current fresh template inside the markers; missing, outdated, or legacy instructions stop with `INSTRUCTION_PROVIDER_CONFIG_REQUIRED`. Personal instructions outside the markers can remain. Existing callers without template_home continue using legacy instructions.

**Operator step:** Complete any necessary login in the fixed provider HOME. Real provider/login acceptance remains outstanding. Codex also needs the separate hook preparation below. Agents must not manufacture approved plans/receipts or borrow the real HOME, an existing App binding, or old global trust.

### Prepare Codex hooks with existing CLIs and human review

Follow this procedure after placing Codex instructions above, using only the same fixed HOME. Real Codex acceptance remains outstanding; development tests use a private copy and fake native RPC. Do not use the old global `codex-plan`/`codex-apply`, the Desktop integration installer, or real App bindings. In another terminal, reset the verified variables. `orrery_codex` is the absolute executable path verified by the operator; pass the same value as `AGENTSTACK_CODEX_BIN` to subsequent launch/resume commands.

```sh
orrery_codex='/absolute/path/to/confirmed/codex'
orrery_client_dir="${orrery_context%/context.json}"
orrery_provider_home="$orrery_client_dir/providers/codex"
orrery_marketplace="$orrery_provider_home/fresh-marketplace"
orrery_plugin_id='agentstack-codex-app@orrery-fresh'
(
  umask 077
  test -d "$orrery_provider_home" && test ! -L "$orrery_provider_home" || exit 1
  test ! -e "$orrery_marketplace" && test ! -L "$orrery_marketplace" || exit 1
  "$orrery_python" "$orrery_install/scripts/build-codex-app-marketplace.py" \
    "$orrery_install/integrations/codex_app" "$orrery_marketplace" \
    --marketplace-name orrery-fresh
)
```

The existing builder deletes an existing destination, so run it **once at an unused dedicated destination**. If it already exists, stop without deleting it and inspect the previous result. The provider HOME must meet the earlier owner-only 0700, canonical/no-symlink requirements.

```sh
CODEX_HOME="$orrery_provider_home" "$orrery_codex" plugin marketplace add \
  "$orrery_marketplace" --json && \
CODEX_HOME="$orrery_provider_home" "$orrery_codex" plugin add \
  "$orrery_plugin_id" --json
```

Check rc0 and each response before proceeding. Verify that native `pluginId` matches the value above and `installedPath` is an actual directory within the same provider HOME. Stop for an unknown path, a path outside that HOME, a symlink, or unknown ownership. Do not guess that the marketplace source path is the installedPath. These commands place the plugin; they do not approve hook trust.

```sh
orrery_plugin_root='/absolute/installedPath/returned/by/native-command'
orrery_hook_plan="$orrery_provider_home/hook-plan.json"
test ! -e "$orrery_hook_plan" && test ! -L "$orrery_hook_plan" && \
  "$orrery_python" "$orrery_install/scripts/codex_plugin_trust.py" plan \
    --codex-binary "$orrery_codex" --shared-codex-home "$orrery_provider_home" \
    --cwd "$orrery_fresh_root" --plugin-root "$orrery_plugin_root" \
    --plugin-id "$orrery_plugin_id" --output "$orrery_hook_plan"
```

The plan creates a private file with `approved: false`. Stop instead of replacing an existing plan; the operator decides whether to preserve it and begin a new review. **The agent stops here and hands over to the operator.** The operator reviews the binary, HOME, cwd, plugin placement, payload, and every hook. Only if approving this version does the operator edit just `approved` to `true` in that same file. No agent self-approval, reused approval from an old plan, or manufactured receipt is allowed. Preserve the file's 0600 permissions and location.

After the operator completes review, apply that approval with the existing CLI.

```sh
"$orrery_python" "$orrery_install/scripts/codex_plugin_trust.py" apply \
  --approved-plan "$orrery_hook_plan" \
  --receipt "$orrery_provider_home/hook-trust-result.json" \
  --shared-codex-home "$orrery_provider_home" \
  --codex-binary "$orrery_codex" --plugin-id "$orrery_plugin_id"
```

Require rc0 and `hook_trust: observed_current`. Stop for `needs_review`, placement/payload changes, or an uncertain response and have the operator review the current contents again. Normal launch grants no approval: it only checks the plan, payload, and native trust in the same HOME. Missing preparation is refused with `CODEX_HOOK_CONFIGURE_REQUIRED` or another explicit error, without falling back to another HOME or the old route. Existing launch disables the plugin's bundled Mail MCP and uses only the same fresh client's proxy. This preparation alone does not complete real Desktop or provider acceptance.

Once preparation is complete, the regular foreground launch form is below. First confirm that old global `AGENTSTACK_CLIENT_CONFIG`/`AGENTSTACK_GLOBAL_SERVER_CONFIG` selections are absent. Preserve the original settings for recovery; do not print credentials to check them.

```sh
orrery_workdir='/absolute/path/to/work-folder'
AGENTSTACK_PYTHON="$orrery_python" AGENTSTACK_RUNTIME_MODE=fresh \
  AGENTSTACK_FRESH_CLIENT_CONTEXT="$orrery_context" \
  "$orrery_install/bin/agent-start" "$orrery_workdir"
```

For Codex, replace `agent-start` with `agent-start-codex`. The launched model verifies its owner through the bound proxy's `runtime_status`; it does not add project, credentials, or owner IDs absent from the schema. Reservation paths are anchored to the actual working folder. Missing/corrupt fresh contexts or expired credentials do not authorize fallback to old registration.

Resume accepts **only native sessions already recorded by this new client**. It does not automatically attach an old Mail session, another client's session, or a run whose termination is unknown. With matching records, the arguments are `agentstack-resume claude "$orrery_workdir" --session-id <recorded-native-UUID>` (`codex` for Codex), with the same fresh environment variables as above. If termination is unknown, stop and let the operator establish execution state; do not rewrite receipts manually.

### Automatic children: fresh routing through existing preregister/spawn entries

Cutover requires automatic children through `/delegate`, New Agent and `/api/spawn`. **Limited fixtures cover the common Claude/Codex entries, managed dsh/Hermes/Local, App adapters and screen API wiring. Manually creating another agent is not a substitute completion condition.** Do not cut over until real-provider, real-environment startup/shutdown and attended acceptance are complete.

The parent's explicit fresh context prepares a separate numeric ID, credential and context for each child. The same `child-client-name` identifies a preparation retry; only identical arguments return to that same child. Changed name/program/model/task-description or parent is refused. Choose a new slot explicitly for a new child; do not create another name after a communication failure.

This example uses Claude. For Codex, use program `codex` and add `--codex` to spawn. Specify the actual provider model. The child uses the same family's provider HOME already prepared by the operator; it never automatically copies hook approval for a new HOME. Missing instructions, binary or current trust stop before spawn. Native transcripts live in that provider HOME; Mail ownership, task and run/session records remain separate for each child. There is no automatic login/hook approval or real-HOME discovery.

```sh
orrery_child_slot='worker_01'
orrery_child_name='ChildWorker'
orrery_child_model='<actual-provider-model>'
orrery_child_task='/absolute/path/to/reserved-task.md'
orrery_child_cwd='/absolute/path/to/child-work-folder'
orrery_child_resource='/absolute/path/to/child-work-folder/target.py'
(
  export AGENTSTACK_PYTHON="$orrery_python" AGENTSTACK_RUNTIME_MODE=fresh
  export AGENTSTACK_FRESH_CLIENT_CONTEXT="$orrery_context"
  "$orrery_install/bin/agentstack-preregister-child" \
    --child-client-name "$orrery_child_slot" --name "$orrery_child_name" \
    --program claude-code --model "$orrery_child_model" \
    --task-description 'bounded child task' --prepare-only && \
  /bin/bash "$orrery_install/hooks/spawn_child.sh" \
    --child-client-name "$orrery_child_slot" --pre-registered "$orrery_child_name" \
    --embed-task --task-file "$orrery_child_task" \
    --resources "$orrery_child_resource" "$orrery_child_cwd"
)
```

Reserve the task file before creating it, outside the fresh Mail private root. Its contents are saved and passed to the native provider as argv, never evaluated as shell code. Never put credentials/bindings in it. Preparation returns token-free JSON with the child name and context. This fresh entry does not accept old `--token-file-out` or project arguments. Use `--unsafe-no-resources` instead of `--resources` only when resources are unnecessary. Adding `--prepare-only` to spawn prepares and pins the task/reservations without starting a provider.

Normal spawn automatically creates a child session through the existing tmux transport. It uses the parent's socket, or only the existing ordinary-socket rules when unspecified. An unprepared server stops with `FRESH_CHILD_TMUX_PREPARE_REQUIRED`; the operator checks the existing tmux setup procedure. There is no input injection into another pane or new watcher/daemon. Initial `accepted` means the handoff was recorded, separately from `native_ready`. Repeating the same task/slot returns the same attempt's state without launching twice. For an unknown result, preserve the task/receipts for operator inspection instead of deleting them to retry. Changed tasks or mixed child identities are refused. The child follows the supplied task and reports through Mail to the specified parent ID in the same instance.

### Codex Desktop App (cutover incomplete)

The bridge socket, worker, spool and Desktop transcript header check are reused. Common child preparation gives root tasks/subagents separate IDs/contexts. Desktop has no CLI nonce and does not enter the CLI session hook. Prepare notifications, cold wake of waiting roots and common external-child launch below. **Real App/login/human trust, real OS managers/Finder GUI and live distribution remain unaccepted; this procedure alone does not complete a cutover.** Development Desktop transcripts/providers/trust are private synthetic fixtures.

| Agent step | Operator step | Verification |
|---|---|---|
| Check core/App versions, interpreter, runner, plugin payload and old bridge stop/recovery procedures | Review plugin changes/hook approvals and decide when to stop the old bridge | Matching fresh adapter; no old binding/credential copies |
| Restore verified variables in a separate terminal and start the ordinary runner below | Confirm Desktop HOME/login and perform approvals personally | Dedicated runtime/HOME/template context match; live replacement is attended |
| Bootstrap with hook-provided session/subagent, verify identity, then use Mail/reservations | Verify in the real Desktop application | Root/subagent IDs differ; unprepared/foreign/revoked/missing or one-sided Move coverage refuses |

In each terminal, restore the same verified `orrery_python`, `orrery_install` and `orrery_context` from earlier steps. The context is a preparation client in the new Mail instance. HOME is the operator-verified existing Desktop HOME, not copied CLI approvals or old App bindings. Use a different short absolute runtime path from the old bridge; stop on conflicting existing settings.

```sh
orrery_app_install='/absolute/path/to/installed/codex_app'
orrery_app_home='/absolute/canonical/Desktop/CODEX_HOME'
orrery_app_runtime='/absolute/short/private/fresh-app-runtime'
test -f "$orrery_app_install/src/agentstack_codex_app/fresh_bridge.py" || exit 1
test -f "$orrery_app_install/run-codex-app-bridge.sh" || exit 1
export AGENTSTACK_PYTHON="$orrery_python" AGENTSTACK_HOME="$orrery_install"
export AGENTSTACK_RUNTIME_MODE=fresh AGENTSTACK_FRESH_CLIENT_CONTEXT="$orrery_context"
export AGENTSTACK_CODEX_APP_INSTALL_DIR="$orrery_app_install"
export AGENTSTACK_CODEX_APP_RUNTIME_DIR="$orrery_app_runtime" CODEX_HOME="$orrery_app_home"
unset AGENTSTACK_CLIENT_CONFIG AGENTSTACK_GLOBAL_SERVER_CONFIG
/bin/bash "$orrery_app_install/run-codex-app-bridge.sh"
```

The operator gives Desktop the same explicit settings. One-second hooks only perform local acceptance/spooling, never registration HTTP; `queued`/`spooled` does not mean preparation is complete. Report unprepared routes rather than escaping through another registration/old transport. Routes store I/A/generation, context references and native metadata, without another credential store. A bootstrapped stdio process cannot switch sessions/subagents; current credentials/routes are rechecked. SubagentStop ends that subagent's use; root Stop means a waiting turn. Stop on settings/HOME/transcript mismatch instead of editing receipts. The operator restores the recorded old version/settings for recovery.

The same bridge worker queries notifications for the current owner/recipient and hands them to PostToolUse through bounded local IPC. Only a receipt returned after hook stdout flush completes notification facts; it does not mark read/ACK or completed work. Uncertain flush/receipt/response delivery leaves pending notifications and may repeat them. Importance filtering precedes the query LIMIT.

Cold wake reuses the existing policy/resume path, resuming only a waiting/dormant root after an observed Stop in the same Desktop HOME/native session. SessionStart, working turns and stopped subagents are not resumed in parallel. The Desktop HOME must contain an **existing human-approved `hook-plan.json`, native trust and plugin payload** matching binary, cwd and plugin ID. If absent, follow the existing Codex hook preparation for that explicit HOME through the human step and verify it; the agent must not manufacture an approval receipt. Restore verified `AGENTSTACK_CODEX_BINARY`/`AGENTSTACK_CODEX_APP_PLUGIN_ID` in each terminal. `AGENTSTACK_CODEX_APP_COLD_WAKE=0` disables wake and does not replace notification/external-child acceptance. Exit code0 confirms notification handoff only; failure/timeout/unknown execution keeps pending facts. Unknown wake execution is not automatically restarted; ask the operator to stop and investigate.

For App external children, **the operator** first prepares a dedicated tmux socket. Start existing tmux in a new short absolute private directory and set `AGENTSTACK_CODEX_APP_CHILD_SOCKET` to its actual socket before starting bridge/Desktop. Later configuration changes refuse. No default socket or other pane is searched. This manual preparation does not replace child registration, task transfer or automatic launch.

Pass the session/subagent confirmed by the same bootstrap to both ordinary `agentstack-preregister-child` and `spawn_child.sh` using `--app-session-id` and, for a subagent, `--app-agent-id`. Do not add owner selectors or credentials. These are the same automatic child entries used by delegate; missing provider preparation/consent/trust refuses. In each terminal, restore verified variables and the explicit App environment.

```sh
orrery_app_session='session-id-from-bootstrap'
orrery_app_subagent='subagent-id-from-bootstrap'
"$orrery_install/bin/agentstack-preregister-child" --app-session-id "$orrery_app_session" --app-agent-id "$orrery_app_subagent" --child-client-name worker --name AppWorker --program claude-code --model explicit-model --task-description 'bounded task' --prepare-only
"$orrery_install/hooks/spawn_child.sh" --app-session-id "$orrery_app_session" --app-agent-id "$orrery_app_subagent" --child-client-name worker --embed-task --task-file /absolute/path/to/task.md --resources note.py /absolute/work/folder
```

For a root task, omit `--app-agent-id "$orrery_app_subagent"` from both commands. Acceptance and native-session establishment are separate. Verify the registered parent context, a distinct child A/context and Mail to that exact parent. Resending unknown execution must not launch another process. Real App/provider acceptance remains for attended cutover.

### Constraints to check before use

- dsh's `backend_file` connection definition is not pinned at prepare time. Do not edit it between preparation and completed generation/launch. Explicit Local URL/key/model checks and Hermes HTTPS requirements remain.
- PC history includes only Claude/Codex user/assistant text from the current native receipt. It reads at most the last 2 MiB, with at most 200 events, 64 KiB per event and 256 KiB total. It omits tool/thinking; dsh/Hermes are explicitly unavailable.
- Missing required input such as the body can currently produce `AUTH_PROOF_INCOMPLETE`. Check required fields and message text first; this code alone does not justify reissuing tokens or changing bindings.
- Ended cleanup retires the same I/A and removes only its pane. Close Terminal/Cockpit views; remaining clients still cause refusal after owned consumers stop. Do not fill partial/unknown outcomes by resending or claim support for retired reactivation or App foregrounding.

## 6. Carry selected content from the archive

Explicitly select the finalized copy's absolute path and search text. **The ban on reading storage.sqlite3 directly still applies.** Give the agent only the following product CLI's output.

```sh
orrery_archive_copy='/absolute/path/to/finalized-archive-copy/storage.sqlite3'
"$orrery_python" -m agentstack_mail.archive_cli \
  --archive-copy "$orrery_archive_copy" --limit 20 --offset 0 search '<literal-search-text>'
"$orrery_python" -m agentstack_mail.archive_cli \
  --archive-copy "$orrery_archive_copy" --limit 20 --offset 0 thread --message-id '<old-message-id>'
"$orrery_python" -m agentstack_mail.archive_cli \
  --archive-copy "$orrery_archive_copy" attachments --message-id '<old-message-id>'
```

Search uses subject/body substrings, not the old FTS query language. A thread is scoped to the selected old message's storage origin. Attachments expose path references and related fields only; the CLI never opens or copies their contents. Original paths may no longer locate moved attachments.

Requests return 1–100 items, at most 8192 body characters per message, within an approximately 512KiB output budget. Follow `has_more` and `next_offset` for further pages; also inspect `body_truncated`, `attachments_truncated`, and `recipients_truncated`. Paging does not retrieve omitted parts of a clipped body. Full-text carryover and full-history export are not guaranteed. Stop on `ARCHIVE_COPY_NOT_FINALIZED` without deleting sidecars.

Send selected text and old-thread quotations as **new Mail**, identifying the old archive as the source. Use the current proxy schema's `send_message` with subject, body, and recipient. Do not pass an old message ID to the new side's `reply_message`. Ambiguous names are refused with candidates; confirm the active recipient's numeric ID and select it explicitly. Do not choose someone by the newest ID or working folder. Do not automatically resend attachment files; include references in the body when needed.

Resending uses a new ID, a new timestamp, and the current sender. It does not carry old unread/read/ACK state, credentials, bindings, delivery state, or thread identity. Before writing a note, **reserve that note's path on the new side**; do not edit without a reservation while waiting for the next resume-work step.

## 7. Reserve files and resume across folders

Use the model's bound `reserve_files` with the actual absolute target paths. When the local caller accepts a relative path, it uses the call's real cwd. `project_key` is not a path anchor. Request at least 900 seconds before editing, renew when needed, and release after finishing. Do not supply credentials, instance, actor, request UUID, or anchor to the model proxy.

From at least two working folders, check send/reply between new owners, the owner's peek/read/ACK, actual notifications, refusal of conflicting reservations on the same file, and current guard coverage. A historical reservation receipt does not authorize an edit. An ASCII-folder success does not replace Japanese-path or Windows-side WSL acceptance.

See the [absolute reservation scope](absolute-reservations.en.md) for the APFS Japanese profile. It accepts ASCII and Unicode-3.2-assigned characters in the specified CJK symbol/Hiragana/Katakana/ideograph/fullwidth/halfwidth ranges. NFC/NFD voiced marks and ASCII/fullwidth Latin case variants identify the same target; NFKC width folding is not applied. Accented Latin, Greek, newer ideographs, emoji, and unknown filesystems remain unsupported; a glob encountering an unsupported name can still stop as unknown. Acceptance in a current Mac's private temporary folder is separate from the real vault cutover check. Do not rename or move work into ASCII folders, or convert the filesystem, as a workaround. This is not arbitrary Unicode or all-filesystem support.

Claude notifications use existing hooks; tmux notifications use the existing watcher and the pane fixed at launch. For manual watcher preparation, also repeat the variable definitions with the same verified values in the separate maintenance terminal. Confirm that `AGENTSTACK_FRESH_CLIENT_CONTEXT`, `AGENTSTACK_CLIENT_CONFIG`, and `AGENTSTACK_GLOBAL_SERVER_CONFIG` are not selected, then use the implemented entry `AGENTSTACK_PYTHON="$orrery_python" "$orrery_install/bin/agentstack-runtime-client" watch-fresh --config "$orrery_config"`. Append `--once` for a single check. Do not depend on PATH or default python3. An ordinary foreground terminal does not automatically acquire a tmux destination. The normal installer renders the same config-only watcher service and removes the client-context key. App and dashboard wiring also has limited fixture coverage, distinct from real App/provider acceptance.

The fresh Claude SessionStart hook hands over notifications only when `source` is the string `startup`. For resume, clear, compact, or an unknown, missing, or non-string `source`, the hook still checks the client, current launch, and owner as before, but it does not fetch, print, or complete notifications, and it leaves read/ACK unchanged; they stay pending for the regular watcher. Handing context over at SessionStart does not by itself guarantee that a native turn starts. PostToolUse handoff is unchanged. Leaving notifications to the watcher does not guarantee a wake. The watcher delivers only when its existing checks pass, including the pinned pane, process births and ancestry, a screen state of idle or working, and an empty input line; it skips approval screens, unknown states, and pending input.

Notification completion records the stdout/pane handoff operation, not read/ACK, understanding, or task completion. External input and the DB update are not guaranteed to happen exactly once; uncertain outcomes can produce another notification. For a Codex patch affecting multiple files, refusal on a later file can leave an earlier file's lease extended. Editing is still refused, but expiry timestamps are not guaranteed unchanged across the entire patch. Claude MultiEdit/NotebookEdit are currently refused.

## 8. Recover by procedure after a failure

Use the service management procedures recorded before cutover to stop the new providers, proxies, Mail, watchers, and restart sources. **Preserve the entire new root too; do not delete it.** Only after the operator chooses recovery, follow the saved procedure using the old version/settings/installation. Keep the archive unchanged and use a separate recovery destination rather than making the read-only archive writable. Stop if the exact destination or startup commands are missing.

For shutdown, record work, stop provider/agent and notification writers, close Cockpit's owned consumers normally, then stop the dashboard and finally Mail. Stop restart sources such as supervisors and launchd/systemd through the recorded procedure too. Normal dashboard SIGTERM closes its private listener and owned Runtime, but stopping only its child can trigger a supervisor restart. Replace/restore the DB only after all writers stop. Restart Mail, then dashboard, then Cockpit, reconnecting to the new epoch. Report stop timeouts or unsafe sockets rather than manually unlinking them.

Before new writes, the old connection can be restored. After new writes, first record new Mail, pending replies, and work in progress. New Mail is not automatically migrated back; this is not lossless rollback. Reacquire reservations instead of relying on expired leases when resuming the old operation, and verify there is no simultaneous old/new execution.

If a write reached the server but its response was lost, the saved request can be replayed. The local caller's `fresh_client replay --context <same-context> --request-id <saved-UUID>` uses the original body, recipients, and absolute paths, while verifying current credentials. Resending under a new UUID or editing the saved request is not a continuation of the original operation. An old experimental context lacking the mode pin is refused rather than repaired by inference: prepare a new client UUID from the same owner binding. That does not transfer old native-session resume records.

Reactivation and App foregrounding are outside this cutover scope. Do not report overall completion until real OS managers/Finder GUI, real provider/App/login/trust/live-install acceptance, support for the actual working paths, final CI and an attended cutover are confirmed. Private Mac checks do not establish acceptance for untested environments such as WSL.
