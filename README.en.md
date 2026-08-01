# claude-agent-stack

[日本語](README.md)

A coordination layer and live telemetry dashboard for local Claude Code and Codex agent teams. It uses [mcp-agent-mail](https://github.com/Dicklesworthstone/mcp_agent_mail) as the source of truth for messages, identities, and file reservations, then overlays tmux runtime state, parent-child lineage, communication history, and remaining context in one interface.

![claude-agent-stack demo](assets/demo.gif)

The central design principle is to make operating rules executable through launchers, hooks, mail, and visualization instead of merely expecting LLMs to coordinate.

## Requirements

- macOS is the primary target; launchers and hooks support the system Bash 3.2
- `python3`, `tmux`, `git`, and `uv`
- Claude Code or the Codex CLI
- `fswatch` (optional; the mail watcher polls without it)
- `fzf` (optional directory picker)
- Ghostty (recommended; falls back to iTerm2, Terminal.app, or `none`)
- Obsidian (optional work-log and deliverables integration; requires `AGENTSTACK_OBSIDIAN_APP`)

On Linux, the dashboard uses a systemd user service when available and falls back to `nohup`. Native Windows is not supported. See [Installation](docs/install.md#動作環境) for details.

## Quick start

```bash
git clone https://github.com/gyroid-eth/claude-agent-stack.git
cd claude-agent-stack
./scripts/install.sh
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
| [Third-party components](docs/third-party.md) | agent-mail, the Rider, and credits |

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
        └──────── mcp-agent-mail ◄┘
                  identity / inbox / reservations
```

The stack does not replace agent-mail. It layers launchers, operational guards, visualization, and a control plane on top. If the dashboard stops, identities, mail, and reservations remain in their source of truth.

## License

This repository uses the **MIT License (with OpenAI/Anthropic Rider)**. The Rider adds restrictions on providing the software to OpenAI, Anthropic, and related parties. See [LICENSE](LICENSE) for the complete terms.

The separate `mcp_agent_mail` component also uses an MIT license with the Rider. See [Third-party components](docs/third-party.md) for the separation model and authoritative references.
