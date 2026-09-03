# ORRERY Telemetry

[日本語](README.md)

A coordination layer and live telemetry dashboard for local Claude Code and Codex agent teams. It uses the bundled ORRERY Mail server as the source of truth for messages, identities, and file reservations, then overlays tmux runtime state, parent-child lineage, communication history, and remaining context in one interface.

![ORRERY Telemetry demo](assets/demo.gif)

The central design principle is to make operating rules executable through launchers, hooks, mail, and visualization instead of merely expecting LLMs to coordinate.

## Supported environments

| Environment | Support |
| --- | --- |
| macOS | **Supported.** Uses launchd and falls back to supervised background mode when the `gui/$UID` domain is unavailable. Launchers and hooks support the system Bash 3.2. |
| Linux | **Unverified (code paths exist).** The installer prefers `systemd --user` and falls back to supervised background mode, but no real Linux host has exercised the `systemctl --user` registration and timer start. What is verified is the unit rendering under CI on ubuntu-latest with `systemctl` replaced by a stub. Please report results from a Linux run as an issue. |
| WSL2 | **Unverified.** By design the localhost dashboard should work and Ghostty click-to-jump should not, but this has not been checked on a real WSL2 install. |
| Native Windows | **Unsupported.** |
| Other operating systems | **Unsupported.** The installer preflight stops before writing. |

Python **3.10 or newer** is required. There is no declared upper bound; the full suite is verified on 3.10, 3.12, 3.13, and 3.14, while CI runs 3.10, 3.12, and 3.14.

The required commands are `git` and `tmux`. `uv` is additionally required only when the installer must provision agent-mail. At runtime, at least one of Claude Code or the Codex CLI is required. `systemctl` enables the Linux user service, with the supervisor as its supported fallback. `fswatch` (mail watcher), `fzf` (directory picker), Ghostty, and Obsidian are optional.

The installer begins by checking the OS, Python, required commands, the ORRERY Mail endpoint (default `127.0.0.1:18765`, state root `~/.agentstack/mail`), and install-directory writability, reporting all detected problems together. An occupied endpoint is expected when an existing `install-state.json` marks an update. On a fresh install, the installer does not guess ownership from the socket: it reuses the listener only after resolving a health response and its canonical database, and stops before its first write for an unrelated or unresolved listener.

Only CI and isolated tests that deliberately replace a platform boundary should bypass an individual check with `AGENTSTACK_PREFLIGHT_SKIP_OS=1`, `AGENTSTACK_PREFLIGHT_SKIP_PYTHON=1`, `AGENTSTACK_PREFLIGHT_SKIP_COMMANDS=1`, `AGENTSTACK_PREFLIGHT_SKIP_PORT=1`, or `AGENTSTACK_PREFLIGHT_SKIP_WRITABLE=1`. A bypass does not supply a dependency or make an unsupported environment supported. See [Installation](docs/install.md#動作環境) for details.

## Quick start

```bash
git clone https://github.com/gyroid-eth/claude-agent-stack.git
cd claude-agent-stack
./scripts/install.sh --project-key /absolute/path/to/coordinated-project
```

Review the installer preview and approve the Claude Code settings and managed-instructions merge. Then:

```bash
export PATH="$HOME/.agentstack/bin:$PATH"

agent-start ~/code/my-project
# or
agent-start-codex ~/code/my-project
```

Open the dashboard from another terminal:

```bash
open http://127.0.0.1:8770/
~/.agentstack/bin/agentstack-doctor
```

`agent-start` gives the tmux session the same name as its agent-mail identity. That unambiguously connects dashboard jumps, mail-signal delivery, and token recovery. See [Installation](docs/install.md) and [Configuration](docs/configuration.md) for other setups.

To connect Codex Desktop root tasks and subagents to the same agent-mail project and dashboard, add the optional [Codex App integration](docs/codex-app.md). Codex CLI-only setups do not need this additional install.

## Feature gallery

### 1. Launchers and identity

`agent-start` and `agent-start-codex` combine identity registration, scientist naming, tmux setup, and CLI startup in one path. Tokens live in mode-`0600` runtime files, while inherited identity variables are cleared to prevent identity hijacking.

<!-- TODO: screenshot: launcher and registered agent -->

### 2. Hooks, mail, and file reservations

Claude Code hooks block unregistered sessions and conflicting writes, while agent-mail inbox signals are reinjected into Claude and Codex REPLs. A single source of truth for mail and reservations keeps coordination intact across UI restarts.

By default, agent-mail queues the audit archive's Git commit asynchronously and returns from a tool once the database update and archive-file writes are complete. Set the kill switch `AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC=false` to restore synchronous commits. A hard shutdown immediately after a response can lose a commit that is still in flight, but its archive files remain in the working tree and the database is unaffected. The next startup commits those files synchronously. See the [agentstack-mail guide](docs/agentstack-mail.md#archive-commit-latency-and-startup-repair) for details and measurement conditions.

<!-- TODO: screenshot: agent-mail notification and reservation -->

### 3. DECK

Cards group running, standby, finished, and gone agents while showing tasks, models, remaining context, latest instructions, and deliverables. History, Output, terminal access, and confirmed EXIT/KILL controls stay in one place.

<!-- TODO: screenshot: DECK view -->

### 4. NETWORK and DIGEST REPLAY

A force graph overlays spawn lineage and agent-mail traffic, with explorable nodes, edges, roles, groups, and a mail drawer. Select multiple agents to replay communication and state changes with speed, HOLD, and TIME-TRAVEL controls.

<!-- TODO: screenshot: NETWORK and DIGEST REPLAY -->

### 5. Control plane and NEW AGENT

The dashboard can EXIT, RESUME, REPLAY, annotate roles, and spawn Claude or Codex children. Registration, task delivery, token creation, and tmux launch follow one auditable sequence.

<!-- TODO: screenshot: NEW AGENT modal -->

### 6. API and customization

Every dashboard view and control is backed by a local HTTP API. Environment variables configure portrait overlays, spawn directories, model catalogs, and the terminal bridge while keeping private assets outside the repository.

Murmurs follow the browser language automatically; override them with `?lang=` / `AGENTSTACK_LANG`, and control visibility with `?murmur=on|off` / `AGENTSTACK_MURMUR=off`.

<!-- TODO: screenshot: API or customized portraits -->

## Documentation

The Japanese documentation is canonical. English versions of the detailed guides are planned.

| Guide | Coverage |
| --- | --- |
| [Installation](docs/install.md) | Install tiers, settings merge, VERSION, TCC, upgrade, and uninstall |
| [Launchers and identity](docs/launchers.md) | `agent-start`, naming, tokens, fail-closed checks, and `CLAUDECODE` |
| [Hooks and operational helpers](docs/hooks.md) | Five Claude event hooks, six launcher/watcher helpers, triggers, blocking, and cleanup |
| [Codex App integration](docs/codex-app.md) | Codex Desktop plugin, Bridge, session-bound MCP, inbox notices, and cold wake |
| [Dashboard](docs/dashboard.md) | DECK, NETWORK, SELECT, REPLAY, NEW AGENT, and embed mode |
| [API reference](docs/api.md) | Every route, query/request fields, and response schemas |
| [Configuration](docs/configuration.md) | `AGENTSTACK_*` environment variables and customization |
| [Troubleshooting](docs/troubleshooting.md) | `NOT CONFIGURED`, services, notifications, spawn, and authentication |
| [Third-party components](docs/third-party.md) | agent-mail, licensing, and credits |

See [CONTRIBUTING.md](CONTRIBUTING.md) before sending code changes.

## How it fits together

```text
Claude Code / Codex CLI
        │ launchers + hooks
        ▼
tmux session ── telemetry ──► dashboard
        │                         ▲
        │                         │ sanitized snapshot
        │                  Codex App Bridge ◄── plugin hooks ── Codex Desktop
        │                         │
        └──────── ORRERY Mail ◄────┘
                  identity / inbox / reservations
```

The bundled ORRERY Mail server is the source of truth, with launchers, operational guards, visualization, and a control plane layered on top. Legacy cutovers never share a writable database or archive. If the dashboard stops, identities, mail, and reservations remain in their source of truth.

## License

This repository uses the **PolyForm Perimeter License 1.0.1**. It is source-available, not open source in the OSI sense. See [LICENSE](LICENSE) for the complete terms.

- You may use, modify, and redistribute it for any purpose.
- You may **not** provide others with a product that competes with this software. Competing covers free distribution, ports to another language, and delivery as a service, library, or plug-in.

Attribution for inherited or derived portions of the bundled service is recorded in its [NOTICE](packages/agentstack_mail/NOTICE.md), and their license is preserved as [UPSTREAM_LICENSE](packages/agentstack_mail/UPSTREAM_LICENSE). AgentStack-authored portions use [AGENTSTACK_LICENSE](packages/agentstack_mail/AGENTSTACK_LICENSE). See [Third-party components](docs/third-party.md) for the boundary.
