# Installation

> 日本語版: [install.md](install.md)

[Back to README](../README.en.md) · [Next: Launchers and identity](launchers.en.md)

This document is for people installing ORRERY Telemetry (this repository) for the first time. It takes three commands to install, two to verify, and one to start the first agent. Migration instructions for previous users of the third-party MCP Agent Mail are collected in the [appendix](#appendix-for-previous-mcp-agent-mail-users) at the end.

## Supported environment

macOS is the primary target. The launchers and hooks are implemented to work with the system Bash 3.2 on macOS.

Required:

- Python 3.10 or newer (`python3`). The full suite has been measured on 3.10 / 3.12 / 3.13 / 3.14. There is no upper bound (CI runs 3.10, 3.12, and 3.14 every time, so incompatibility with a newer Python will fail there)
- `tmux`
- `git`
- `uv` (used to create the Python environment for the bundled ORRERY Mail)
- Claude Code or Codex CLI

Optional:

- `fswatch`: mail watcher. Falls back to polling every two seconds when absent; notifications still arrive
- `fzf`: directory picker for a launcher without arguments. The current directory is used when absent
- Ghostty: click-to-jump and window titles. Falls back to iTerm2, Terminal.app, or `none`. Only Ghostty can raise an existing window; iTerm2 and Terminal.app open a new window for every jump
- Obsidian: vault / Daily Note integration for `/log` and links that open Output items inside a vault. `/log`'s Obsidian mode is enabled only when `AGENTSTACK_OBSIDIAN_APP` is set; the installer does not set it. Without it, `/log` writes to local `logs/`, and the dashboard displays a generic project log as a non-link item

On macOS, the resident-service path is chosen by the actual result of bootstrapping into launchd's `gui/$UID` domain. If bootstrap is unavailable while the display sleeps, in an SSH-only environment, or for another reason, installation automatically switches to supervised-background mode, which detects and restarts an exited dashboard server. On Linux, the implementation uses a systemd user service or the same supervised-background mode when unavailable, but it is unverified on a real Linux host; CI only tests unit generation with a stubbed `systemctl`. WSL2 is also unverified. By design, the localhost dashboard should work while Ghostty click-to-jump should not. Native Windows is unsupported.

An explicitly specified `AGENTSTACK_PYTHON` is also checked for Python 3.10 or newer. When unspecified, the installer checks `python3` on PATH, then also searches versioned commands, `/opt/homebrew/bin/python3`, and `/usr/local/bin/python3` if needed. If no compatible interpreter exists, it reports the inspected versions and paths and stops before generating service files.

## Installation

```bash
git clone https://github.com/gyroid-eth/orrery-telemetry.git
cd orrery-telemetry
./scripts/install.sh --project-key /absolute/path/to/your-project
```

Pass the absolute path of the project where agents will work to `--project-key`, not the checkout of this repository. It is required on first install, and the installer stops without writing anything if it is omitted. Later installations inherit the previous value from `~/.agentstack/env.sh`, so it can be omitted.

The installer previews three changes and asks for `yes` for each.

1. Claude Code MCP registration (add `orrery-mail` to `~/.claude.json`)
2. Claude Code settings (append hooks and permissions to `~/.claude/settings.json`)
3. Managed instruction blocks (between markers in the project's `CLAUDE.md` and `~/.codex/AGENTS.md`)

All existing content is preserved, and a backup from before each merge is stored in `~/.agentstack/backups`. An item answered with `no` can be installed separately later with a helper; see [Using agent-mail from Claude Code](#using-agent-mail-from-claude-code).

The installer places:

- the dashboard, launchers, hooks, skills, bundled ORRERY Mail, `env.sh`, `VERSION`, and `install-state.json` under `~/.agentstack/`
- `~/.claude/skills/delegate` and `~/.claude/skills/log`, as symlinks into `~/.agentstack/skills/`
- two resident services: the dashboard on port 8770 and ORRERY Mail at `http://127.0.0.1:18765/mcp`, with state under `~/.agentstack/mail`. Each is registered with launchd on macOS or started in supervised-background mode when registration is unavailable

`env.sh` uses mode `0600` and contains no token. Shell dotfiles are not changed.

### Verification

```bash
~/.agentstack/bin/agentstack-doctor
~/.agentstack/bin/agentstack-selftest
open http://127.0.0.1:8770/
```

`agentstack-doctor` reports separately whether required files and settings are present, whether the dashboard's `/api/version` actually responds, and whether launchd / systemd registration and execution state are healthy. When the endpoint responds but the manager is not running it, the result is `unmanaged-background`.

`agentstack-selftest` registers two real agents, exchanges messages, holds a file reservation, and confirms that the dashboard reads the result from the same database. Run it at least once after installation.

### Starting the first agent

A Claude Code session opened before installation does not rescan newly installed skills. Run `/exit` in the existing session, then start a new terminal and launch with the project path.

```bash
export PATH="$HOME/.agentstack/bin:$PATH"
agent-start /path/to/your-project
# Codex CLI なら
agent-start-codex /path/to/your-project
```

`agent-start` creates a tmux session with the same name as the agent-mail identity. Dashboard jumps, mail notifications, and token recovery are joined by this name. In the launched Claude Code session, invoke skills with a leading slash, as in `/delegate`. See [Skills and file reservations](launchers.en.md#skills-2-and-file-reservations) for the first child launch.

## Non-interactive installation (`--assume-yes`)

When installing from CI or a script, the four approvals (Claude settings merge, the `~/.claude.json` MCP entry, the Codex `AGENTS.md` block, and the Claude `CLAUDE.md` block) are skipped with warnings by default. The following option may be used only when the **user personally** reviewed the repository and preview and has granted approval in advance.

```bash
./scripts/install.sh --project-key /absolute/path/to/your-project --assume-yes
```

`--assume-yes` (short form `-y`; environment variable `AGENTSTACK_ASSUME_YES=1` is equivalent) grants approvals in advance; it is not `--force`. Python older than 3.10, a dashboard-port conflict, multiple or absent existing agent-mail database candidates, disagreement with the running server, and automatic-setup failure still stop installation. Every automatically approved item is printed separately as an `assume-yes:` line. Agents and automation must not add this option “for convenience” without the user's explicit choice. The command-line option takes precedence over the environment variable and is not persisted in generated `env.sh`.

## Installation tiers and options

| Invocation | Tier | Behavior |
| --- | --- | --- |
| `./scripts/install.sh` | Tier 1 / default | Install all payloads and Claude skill links. Merge hooks, permissions, and Codex / Claude managed blocks only when approved after preview |
| `./scripts/install.sh --dashboard-only` | Tier 0 | Dashboard and helpers only. Do not install hooks, skills, or Codex / Claude templates |
| `./scripts/install.sh --scoped` | Tier 2 placeholder | Install payloads but do not change user settings / managed documents |
| `./scripts/install.sh --dry-run` | preview | Display planned changes without changing files or services |

`--dashboard-only` and `--scoped` are mutually exclusive. An unknown option or missing value stops before any changes.

```text
--install-dir PATH      default: ~/.agentstack
--project-key PATH      first install: required / re-install: existing env.sh
--port PORT             default: 8770
--label-prefix PREFIX   default: org.agentstack
--terminal MODE         auto | ghostty | iterm | terminal | none
--spawn-dirs PATHS      NEW AGENT の launch directory preset（`:` 区切り）
--spawn-roots PATHS     directory typeahead が閲覧できる root（`:` 区切り）
--codex-approval MODE   Codex child の `--ask-for-approval`（never | on-request | on-failure | untrusted、既定 never）
--codex-network MODE    Codex child の sandbox network（on | off、既定 on）
--codex-add-dirs PATHS  Codex child に追加で書込を許す root（`:` 区切り）
--retire-legacy-mail    付録参照（以前の MCP Agent Mail を退役させる）
-y, --assume-yes        approval prompts only; validation errors remain fatal
```

An explicit `--project-key` always has highest priority, followed by environment variables `AGENTSTACK_PROJECT_KEY` / `PROJECT_KEY`, then an existing `env.sh` at the install destination. `--spawn-dirs` / `--spawn-roots` follow the same precedence and retain their previous values on reinstall; see “Spawn directory” in [configuration](configuration.en.md). `--bin-dir` is not a public option. The installer calls `agentstack-merge-settings --bin-dir "$INSTALL_DIR/bin"` internally to expand `__AGENTSTACK_BIN_DIR__` in the permissions template.

## Merging settings, permissions, and Claude skills

The Tier 1 merge uses the JSON parser in `scripts/lib/merge_settings.py`.

- Preserve existing hooks, permissions, and other user settings
- Append only AgentStack values, without duplicates
- Save a pre-merge settings backup in `~/.agentstack/backups`
- Record added entries and the change result in the manifest
- Idempotently update only content between managed-block markers
- Treat `install-state.json` as canonical for uninstall scope

Permission `deny` entries are limited to **irreversible operations with no recovery mechanism**. Even destructive operations are left out of both allow and deny when they are recoverable, leaving them to runtime human approval. An operation is also not added to deny when the same state can be reached through another allowed tool, because doing so would provide no safety. The current three denied operations are `hard_delete_agent`, `hard_delete_project`, and `purge_old_messages`.

The installer parses and merges structure rather than performing simple string replacement so that reinstall and uninstall do not sweep up user settings.

`skillsDirectories` is not a Claude Code setting, and the installer does not add a new value. Canonical skill payloads remain in `~/.agentstack/skills/<name>`, with absolute symlinks from Claude Code's standard path at `~/.claude/skills/<name>`.

```text
~/.claude/skills/delegate -> ~/.agentstack/skills/delegate
~/.claude/skills/log      -> ~/.agentstack/skills/log
```

An existing symlink to the same AgentStack payload is reused and recorded as owned in the manifest. Because the link becomes invalid with the payload, it is removed on uninstall. An existing same-name file, directory, or symlink to another target is preserved with a warning and is not recorded as owned. Uninstall compares the manifest path with the actual symlink target and removes only an owned symlink that points to the AgentStack payload. A path replaced by the user with a file or directory, or a retargeted symlink, remains.

On systems where an old installer added `~/.agentstack/skills` to `skillsDirectories`, a reinstall with the Tier 1 settings merge approved removes only that old AgentStack entry. Other user values in the same array and all other settings are preserved.

The installer does not change shell dotfiles. Within the project, it updates only content between managed markers in `CLAUDE.md`, and only after a Tier 1 preview is approved; it changes no other file. The default location for Claude Code user settings is `~/.claude/settings.json` and can be changed with `AGENTSTACK_CLAUDE_SETTINGS`.

## Using agent-mail from Claude Code

The `/delegate` skill allows `mcp__orrery-mail__*` tools, and the Claude Code user-scope MCP server name is fixed as **`orrery-mail`**.

The Tier 1 installer parses `mcpServers` in `AGENTSTACK_CLAUDE_JSON` (default `~/.claude.json`) as structure and adds or updates only the following entry, preserving all other servers and project settings.

```json
{
  "mcpServers": {
    "orrery-mail": {
      "type": "http",
      "url": "http://127.0.0.1:18765/mcp"
    }
  }
}
```

The diff preview replaces bearer tokens with `<redacted>`. Only an interactive `yes` or a user-explicit `--assume-yes` authorizes an atomic mode-`0600` write, with the original file saved in `~/.agentstack/backups`. A non-interactive unapproved run does not write; the installer and `agentstack-doctor` show safe preview / apply commands. `agentstack-selftest` checks this fixed name, endpoint, and authorization registration in addition to the HTTP server itself.

If registration is absent or you answered `no` during installation, follow the doctor's output to preview it.

```bash
~/.agentstack/bin/agentstack-doctor
```

For Codex children, the launcher automatically generates a child-scoped MCP proxy configuration. To use top-level Codex CLI through `agent-start-codex`, add the following once to `$CODEX_HOME/config.toml`. The bootstrap reads `MCP_AGENT_MAIL_TOKEN` into process environment, so the token itself does not need to be stored in TOML.

```toml
[mcp_servers.orrery-mail]
url = "http://127.0.0.1:18765/mcp"
```

The key is fixed as `[mcp_servers.orrery-mail]`.

### Managed instruction helper

The helpers used by Tier 1 to preview / merge can also run independently.

```bash
~/.agentstack/bin/agentstack-codex-setup --print
~/.agentstack/bin/agentstack-claude-setup --print
```

`--print` only displays the target and the block with placeholders resolved; it makes no changes. With no arguments, the helper backs up the existing file and installs / updates only the AgentStack block between markers.

```bash
~/.agentstack/bin/agentstack-codex-setup
~/.agentstack/bin/agentstack-claude-setup
```

Use `--uninstall` on each helper to remove only its block. Codex targets `$CODEX_HOME/AGENTS.md`; Claude targets the `CLAUDE.md` selected by `AGENTSTACK_CLAUDE_MD_SCOPE=project / global / both`. Existing content outside the markers is preserved.

## ORRERY Mail service handling

The bundled ORRERY Mail runs with `AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough`, which registers exactly the identity requested by a launcher. Its default endpoint is `http://127.0.0.1:18765/mcp`, and its state root is `~/.agentstack/mail`. It does not use an HTTP bearer; each agent's owner token is handled through a tool argument / local proxy.

If something already responds at the endpoint, the installer reuses it only when it is ORRERY Mail returning the same database. A listener returning another database is not reused, and installation stops before the first write. In a fresh environment, the installer places the bundled package's virtual environment under a candidate ID, initializes empty state, and continues only after confirming that the health response returns the configured database; see the [agentstack-mail document](agentstack-mail.md) for the internal structure.

The following controller operates the service. The runner restarts itself five seconds after a crash, while the controller checks the PID file, endpoint, and database and refuses duplicate startup or stopping an unrelated process.

```bash
~/.agentstack/bin/agentstack-mailctl start
~/.agentstack/bin/agentstack-mailctl status
~/.agentstack/bin/agentstack-mailctl stop
~/.agentstack/bin/agentstack-mailctl restart
```

Even if dashboard service registration or a health check fails, payload generation, approved managed blocks, and `install-state.json` still complete; the installer finishes by showing a warning and the manual command for supervised background. If mail-service provisioning or database health fails, installation stops. Check the actual dashboard residency mode with `~/.agentstack/dashboard/agentctl.sh status` and `agentstack-doctor`.

## VERSION

The repository-root `VERSION` is canonical. The installer copies it into the install root.

`GET /api/version` resolves the version in this order:

1. `VERSION` adjacent to the installed artifact
2. Repository `VERSION`
3. `git describe --tags --always --dirty`
4. `unknown`

Do not copy only the dashboard HTML; update `VERSION` through the installer as well. This keeps the distributed artifact and displayed version consistent.

## macOS TCC / Full Disk Access

`~/Desktop`, `~/Documents`, and `~/Downloads` are protected by macOS TCC. If a root agent starts from a terminal without Full Disk Access, that terminal identity propagates to tmux and its descendants, and only a child agent may encounter `EPERM`.

Remedies:

1. Start the root agent from a terminal with Full Disk Access
2. Or move the project outside protected locations
3. After changing context, recreate the existing tmux server / session

The launcher warns about this state. If needed:

```bash
export AGENTSTACK_TCC_GUARD=0
export AGENTSTACK_TCC_DIRS="$HOME/Desktop:$HOME/Documents:$HOME/Downloads"
```

The canonical `AGENTSTACK_TCC_DIRS` syntax is colon-separated. Legacy whitespace-separated values without a colon remain accepted for compatibility.

Do not attempt to repair these permission errors only with `chmod`. The deciding identity is the originating application, not the file mode.

## Upgrade

```bash
git pull
./scripts/install.sh
~/.agentstack/bin/agentstack-doctor
```

The installer updates payloads and `VERSION`, reregisters services, and previews managed merges again. It validates and reuses the bundled ORRERY Mail candidate and state. `--project-key` inherits the previous value.

**Keep the agent-mail server running during an in-place upgrade.** The real database path resolved from the running listener takes precedence over filesystem candidate discovery. Stopping agent-mail first falls back to candidate discovery, and the installer stops rather than risk choosing incorrectly in an environment with several databases.

If the dashboard port is held by a process under the current AgentStack launchd job or supervised-background pidfile, the installer verifies ownership and replaces that dashboard with the new payload. It still stops if an unrelated process holds the same port.

Service environment is written into plist / unit files during installation. Changing only `~/.agentstack/env.sh` does not affect an existing service, so rerun the installer or update the service definition too.

## Uninstall

```bash
~/.agentstack/bin/agentstack-uninstall --dry-run
~/.agentstack/bin/agentstack-uninstall
```

The uninstaller targets only files, services, and settings changes recorded in `install-state.json`.

- Structurally remove merged Claude settings entries
- Remove AgentStack-owned files
- Remove only owned directories that become empty
- Preserve ORRERY Mail state / database and the runtime directory (annotations, tokens, session state / logs) by default

Legacy `dashboard/annotations.json` is not included among payload-owned files because it is user state. During upgrade, the installer automatically migrates it to `$AGENTSTACK_RUNTIME_DIR/annotations.json` before copying payloads. It remains in the runtime directory after a normal uninstall.

To remove retained data too:

```bash
~/.agentstack/bin/agentstack-uninstall --purge-data
```

`--purge-data` also targets only exact paths recorded in the manifest; it does not remove a home directory or an unrecorded path. The runtime directory is a purge path, so this option also removes annotations.

## Appendix: for previous MCP Agent Mail users

This section does not apply to first-time users. It is for people who ran the third-party [MCP Agent Mail](third-party.md), on which the bundled ORRERY Mail is based, as their own launchd job.

### Retiring the legacy launchd mail service

When a legacy `mcp_agent_mail` launchd job holds the endpoint, run the installer with explicit `--retire-legacy-mail`. **Before** checking whether it can reuse the existing listener as ORRERY Mail, the installer compares known legacy labels with plist executables, boots out the matching job, and parks its plist in `~/.agentstack/parked-launchd/`. Without the flag, it does not stop the service; it reports the detected label and required flag, then stops.

`--dry-run --retire-legacy-mail` does not actually stop the job, but first displays the retirement plan and then shows the bundled ORRERY Mail provisioning plan under the assumption that the listener will be retired. Repeated legacy scans within the same installer process do not boot out or park the job twice.

If Claude Code's old `mcp-agent-mail` key points to the same endpoint, an approved settings merge moves it to the `orrery-mail` key. An old key pointing to another endpoint remains as an unrelated entry.

### Manual migration of the legacy database

To retain old state (database, archive, and signals), first stop the old writer and confirm all three paths. The installer does not migrate automatically. With the destination still absent, manually run the migration CLI's `copy` and `verify` from the repository checkout, then run the installer.

```bash
LEGACY_DB="/absolute/path/to/the-stopped-legacy-database"
LEGACY_ARCHIVE="/absolute/path/to/the-stopped-legacy-archive"
LEGACY_SIGNALS="/absolute/path/to/the-stopped-legacy-signals"
DESTINATION="$HOME/.agentstack/mail"

uv run --project packages/agentstack_mail agentstack-mail-migrate copy \
  --source-db "$LEGACY_DB" \
  --source-archive "$LEGACY_ARCHIVE" \
  --source-signals "$LEGACY_SIGNALS" \
  --destination-root "$DESTINATION"

uv run --project packages/agentstack_mail agentstack-mail-migrate verify \
  --source-db "$LEGACY_DB" \
  --source-archive "$LEGACY_ARCHIVE" \
  --source-signals "$LEGACY_SIGNALS" \
  --destination-root "$DESTINATION"

./scripts/install.sh
```

The migration helper and service controller reject configurations that share a source and destination database / archive. At the 2026-08-12 cutover, this procedure actually transferred and verified about 60,000 database and archive records in total. Keep the old writer stopped from copy through verify.

### Rollback

The installer does not automatically switch back to the third-party version. If necessary, stop ORRERY Mail and recover manually with the migration and settings backups.

## Related documentation

- [Launchers and identity](launchers.en.md)
- [Hooks and operational helpers](hooks.en.md)
- [Codex App integration](codex-app.en.md)
- [Configuration](configuration.en.md)
- [Troubleshooting](troubleshooting.en.md)
- [Third-party components](third-party.md)
