# ORRERY Telemetry

[日本語](README.md)

ORRERY Telemetry lets coding agents from different vendors, such as Claude Code, Codex CLI, and Gemini, work together as one team, shows what they are doing on a single screen, and gives you a way to step in when you need to. The bundled infrastructure records the messages agents exchange, the file reservations that keep them from overwriting each other, and the lineage of who spawned whom, and the dashboard turns that into something a person can follow.

![ORRERY Telemetry demo](assets/demo.gif)

**Start with the demo**: [agentstack-demo.pages.dev](https://agentstack-demo.pages.dev/) is the real dashboard driven by scripted data. It loops through agents starting, exchanging messages, spawning a child and finishing, in four minutes, with captions saying what is happening. Nothing to install; it gives you a feel for the experience on a screen close to the real thing. Watch it once through before continuing.

## Who this is for

- **People who run coding agents, not chat**: not chat in the ChatGPT sense, but agents like Claude Code or Codex that take an instruction, write code, change files, and run commands on their own. It does not matter whether you drive them from a terminal or from a desktop app such as Claude Desktop or the ChatGPT app.
- **A good fit**: you run two or more agents at the same time and are tired of switching terminal tabs to find out who is doing what.
- **Not a fit**: one agent is enough for you. The value here is in coordinating several agents and watching them.
- **Supported agents**: Claude Code and Codex CLI are the core. Codex Desktop tasks and subagents, and Google Antigravity / Gemini, are optional providers you can add after the core install, and they appear on the same dashboard.

## Six words

These six terms are all you need to read this README and the guides.

| Term | Meaning |
| --- | --- |
| agent | One Claude Code / Codex CLI session running in a terminal. Each one gets a scientist's name. |
| child | Another agent that an agent started by asking it to do a job. The one that asked is the parent. A parent creates a child with `/delegate`; when the child is done it reports back to the parent and goes away. |
| ORRERY Mail | The small bundled server through which agents message each other and that manages names and file reservations. |
| dashboard | The page you open in a browser. It shows every agent's state, the parent-child tree, and the messages going back and forth. |
| project key | The absolute path of the project folder the agents work on. It is the key that tells which project an agent belongs to. |
| skill | A playbook that tells an agent "when asked like this, follow these steps". You call one with a leading slash, as in `/delegate`. This tool ships two: `/delegate` and `/log` ([explained below](#the-two-bundled-skills)). |

## Quick start

The steps are the same on macOS and Windows. You need Python 3.11 or newer, `git`, `tmux`, and `uv`; on macOS, `brew install tmux uv` covers the last two.

**On Windows**: install inside the Ubuntu of WSL2. Ubuntu is Linux, so the steps below apply unchanged; the dashboard opens in your Windows browser and agent terminals open as Windows Terminal tabs. The [WSL2 section of the install guide](docs/install.en.md#installing-on-windows-wsl2) walks from setting up WSL2 to logging in to Claude Code / Codex, so Windows readers should open that first.

### 1. Install

```bash
git clone https://github.com/gyroid-eth/orrery-telemetry.git
cd orrery-telemetry
./scripts/install.sh --project-key /absolute/path/to/your-project
```

`--project-key` is the absolute path of the project folder you want the agents to work on, not the path of this repository.

Before touching your Claude Code / Codex configuration the installer shows each change and asks for `yes` four times in total. Existing settings are kept, and a backup of the previous state goes to `~/.agentstack/backups`. Add `--dry-run` to see the planned changes without applying them.

**Success**: the last line reads `Install complete: http://127.0.0.1:8770/` and `~/.agentstack/` exists.

### 2. Check that it works

```bash
export PATH="$HOME/.agentstack/bin:$PATH"
agentstack-doctor
agentstack-selftest
```

**Success**: every `agentstack-doctor` line starts with `ok:` (a `warn:` line has its fix printed right under it), and `agentstack-selftest` ends with `self-test passed: two agents registered, exchanged messages, ...`. If either stops, go to the matching section of [Troubleshooting](docs/troubleshooting.en.md).

### 3. Start the first agent and watch it on the dashboard

```bash
agent-start ~/code/my-project          # for Codex CLI: agent-start-codex ~/code/my-project
```

Open the dashboard from another terminal.

```bash
open http://127.0.0.1:8770/
```

**Success**: your usual Claude Code or Codex starts, and one card with a scientist's name appears in the dashboard's DECK, showing the model and remaining context.

### 4. Spawn one child

Inside the running agent, ask for a child. This is the same in Claude Code and Codex.

```text
/delegate reply with your name and today's date
```

**Success**: a second card appears on the dashboard with a line from the parent to the child. When the child finishes, a "done" message arrives in the parent's terminal. The NETWORK tab shows the messages passing between the two.

### 5. Play shiritori as an end-to-end check

The quickest way to confirm the whole install at once is a game of shiritori (Japanese word chain) between a Claude Code agent and a Codex child. Name registration, ORRERY Mail round trips, notification injection, and dashboard rendering all have to work for even one round to complete.

```text
/delegate spawn one Codex child and play shiritori with it: exchange one word per turn over ORRERY Mail, and report the result after ten rounds
```

If you start from Codex, make the child a Claude Code agent. Either way, the point is to see the game go round between agents from two vendors.

**Success**: lines keep flowing back and forth between the two agents in NETWORK, and the "last instruction" on both DECK cards updates word by word. On the author's machine each turn took about five to six seconds per agent ([post with video](https://x.com/i/status/2095650715008168255), played at double speed).

Once you are here, just use your agents as usual. See [Installation](docs/install.en.md) and [Configuration](docs/configuration.en.md) for settings, and [Delegation and child agents](docs/delegation.en.md) for how children work.

## The two bundled skills

A skill is a playbook handed to an agent. Claude Code discovers them under `~/.claude/skills/` and, when you type one with a leading slash such as `/delegate`, follows its steps. In Codex, the managed instructions the installer writes to `~/.codex/AGENTS.md` point at the same playbook, so typing `/delegate` there runs the same steps.

### `/delegate`: hand a job to a child

Type `/delegate <what you want done>` and the agent starts one new child, passes it the request, watches until it finishes, and collects the result. Underneath, it registers the child's name, reserves the files it will touch, creates the tmux session, and receives the completion report as one sequence, so the child is on the dashboard from the moment it starts and never collides with another agent on the same file.

Both Claude Code and Codex have a similar built-in feature called subagents, but children made that way do not appear on the dashboard. A child you want ORRERY Telemetry to watch must be created with `/delegate`. The difference is explained in [Delegation and child agents](docs/delegation.en.md).

### `/log`: keep a record of the session

Type `/log` and the agent writes one Markdown file under `logs/` summarizing what was decided, which files changed, what was verified, and what comes next. Obsidian users can set one environment variable to have it written inside the vault and linked from the Daily Note ([Configuration](docs/configuration.en.md)). These logs are what the Output panel of each dashboard card lists.

Where the skills live and how they are wired is covered in [Launchers and identity](docs/launchers.en.md#skills-2-and-file-reservations).

## What you see

### DECK

One card per agent. It shows running / standby / finished / gone, what the agent is doing now, the model, remaining context, the last instruction, and its artifacts. From the card you can open the terminal or exit the agent behind a two-step confirmation.

![DECK view](docs/img/deck.jpg)

### NETWORK and DIGEST REPLAY

A graph of who spawned whom and who messaged whom. Select several agents and you can replay their messages and state changes at different speeds, and travel back in time.

![NETWORK view](docs/img/network.jpg)

![DIGEST REPLAY](docs/img/digest-replay.jpg)

### NEW AGENT

Start a new agent from the dashboard: pick Claude / Codex, the model, the working folder, and the task, then press `Spawn`. Exiting, resuming, and labeling the role of existing agents happen on the same screen.

![NEW AGENT modal](docs/img/new-agent.jpg)

### What runs underneath

- **ORRERY Mail**: one place for agent names, inboxes, and file reservations. It remains the source of truth even if the dashboard goes down.
- **Launchers**: `agent-start` registers the name, creates the tmux session, and starts the CLI in one step, so dashboard jumps and notification targets resolve to exactly one place.
- **Hooks**: Eight Claude event hooks stop unregistered sessions and unreserved writes, and inject arriving messages into the agent's prompt.

Details of these mechanisms and the API are in [Hooks](docs/hooks.en.md), [Launchers](docs/launchers.en.md), and the [API reference](docs/api.en.md). Every dashboard view and control is available through the local HTTP API.

## Supported environments

| Environment | Support |
| --- | --- |
| macOS | Supported |
| Windows (WSL2) | Supported. Verified on Windows 11 + Ubuntu 26.04 / WSL 2.7 from install through spawning a child and jumping to a terminal from the dashboard. Steps: [WSL2 section of the install guide](docs/install.en.md#installing-on-windows-wsl2) |
| Linux | Field reports received. On Ubuntu 24.04 / tmux 3.4, install, the dashboard, and Codex agents worked, and the two problems found there ([#26](https://github.com/gyroid-eth/orrery-telemetry/issues/26), [#27](https://github.com/gyroid-eth/orrery-telemetry/issues/27)) are fixed. The author has not verified `systemd --user` registration, so please report results as issues |
| Windows native (without WSL2) | The supported route is WSL2, but community contributions provide an experimental PowerShell helper that starts Mail and the dashboard, and a native Codex child launcher ([startup helper](docs/windows-local.en.md), [Codex launcher](docs/windows-codex-launcher.en.md)). Policy: [#3](https://github.com/gyroid-eth/orrery-telemetry/issues/3) |

Python 3.11 or newer, `git`, `tmux`, and `uv` are required, and at runtime at least one of Claude Code or Codex CLI. The installer checks these before writing anything and stops without changes if something is missing. The details of the checks and the optional dependencies (`fswatch`, `fzf`, Ghostty, Obsidian) are in the [install guide's environment section](docs/install.en.md#supported-environment).

## Documentation

The Japanese documentation is canonical. The main guides have English versions.

| Guide | Coverage |
| --- | --- |
| [Installation](docs/install.en.md) | Environment, install details, WSL2, upgrade / uninstall |
| [Launchers and identity](docs/launchers.en.md) | `agent-start`, naming, tokens, `CLAUDECODE` |
| [Delegation and child agents](docs/delegation.en.md) | How children differ from built-in subagents and how to tell which one is running |
| [Hooks and operational helpers](docs/hooks.en.md) | Eight Claude event hooks, triggers, block / release / cleanup |
| [Codex App integration](docs/codex-app.en.md) | Putting Codex Desktop root tasks / subagents on the same dashboard |
| [Google Antigravity / Gemini provider](docs/antigravity.en.md) | Installing the optional provider and its limits |
| [Dashboard](docs/dashboard.en.md) | DECK, NETWORK, SELECT, REPLAY, NEW AGENT, embed |
| [API reference](docs/api.en.md) | Every route, query / request fields, response schemas |
| [Configuration](docs/configuration.en.md) | `AGENTSTACK_*` environment variables and customization |
| [Troubleshooting](docs/troubleshooting.en.md) | `NOT CONFIGURED`, services, notifications, spawn, authentication |
| [Third-party components](docs/third-party.md) | ORRERY Mail, licensing, credits |

For the internals of the bundled server see the [ORRERY Mail design document](docs/agentstack-mail.en.md), and see [CONTRIBUTING.md](CONTRIBUTING.md) before sending code changes.

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

The bundled ORRERY Mail server is the source of truth, with launchers, operational guards, visualization, and a control plane layered on top. If the dashboard stops, identities, mail, and reservations remain in their source of truth.

## License

This repository uses the **PolyForm Perimeter License 1.0.1**. It is source-available, not open source in the OSI sense. See [LICENSE](LICENSE) for the complete terms.

- You may use, modify, and redistribute it for any purpose.
- You may **not** provide others with a product that competes with this software. Competing covers free distribution, ports to another language, and delivery as a service, library, or plug-in.

Attribution for inherited or derived portions of the bundled service is recorded in its [NOTICE](packages/agentstack_mail/NOTICE.md), and their license is preserved as [UPSTREAM_LICENSE](packages/agentstack_mail/UPSTREAM_LICENSE). Portions written for ORRERY Telemetry ("AgentStack" in the file name is the former project name) use [AGENTSTACK_LICENSE](packages/agentstack_mail/AGENTSTACK_LICENSE). See [Third-party components](docs/third-party.md) for the boundary.
