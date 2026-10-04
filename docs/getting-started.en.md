# Getting started with Telemetry on its own

[日本語](getting-started.md) · [Documentation index](README.en.md) · [README](../README.en.md)

For the usual entry point, use the [ORRERY cockpit quick start](https://github.com/gyroid-eth/orrery/blob/master/README.en.md#quick-start). One line installs cockpit and Telemetry together; try Your first flight → help map → Full tour on screen. This page is the standalone Telemetry procedure and a reference for background and terms.

## Who this is for

- **People who run coding agents, not chat**: not chat in the ChatGPT sense, but agents like Claude Code or Codex that take an instruction, write code, change files, and run commands on their own. It does not matter whether you drive them from a terminal or from a desktop app such as Claude Desktop or the ChatGPT app.
- **A good fit**: you run several agents and find yourself acting as the go-between: pasting Codex output into Claude Code, or hopping between apps and tabs to check on each one. You want that manual relay gone and the whole picture on one screen.
- **Not a fit**: one agent is enough for you. The value here is in coordinating several agents and watching them.
- **Supported agents**: Claude Code and Codex CLI are the core. Codex Desktop tasks and subagents, and Google Antigravity / Gemini, are optional providers you can add after the core install, and they appear on the same dashboard.


## Six words

These six terms help you read this standalone procedure and the reference guides.

| Term | Meaning |
| --- | --- |
| agent | One Claude Code / Codex CLI session running in a terminal. Each one gets a scientist's name. |
| child | Another agent that an agent started by asking it to do a job. The one that asked is the parent. A parent creates a child with `/delegate`; when the child is done it reports back to the parent. See [what happens after completion](dashboard.en.md#after-a-child-completes). |
| ORRERY Mail | The small bundled server through which agents message each other and that manages names and file reservations. |
| dashboard | The page you open in a browser. It shows every agent's state, the parent-child tree, and the messages going back and forth. |
| project key | The absolute path of the project folder the agents work on. It is the key that tells which project an agent belongs to. |
| skill | A playbook that tells an agent "when asked like this, follow these steps". You call one with a leading slash, as in `/delegate`. This tool ships two: `/delegate` and `/log` ([playbook details](launchers.en.md#skills-2-and-file-reservations)). |


## Standalone installation and verification

The steps are the same on macOS and Windows. You need Python 3.11 or newer, `git`, `tmux`, and `uv`; on macOS, `brew install tmux uv` covers the last two.

**On Windows**: install inside the Ubuntu of WSL2. Ubuntu is Linux, so the steps below apply unchanged; the dashboard opens in your Windows browser and agent terminals open as Windows Terminal tabs. The [WSL2 section of the install guide](install.en.md#installing-on-windows-wsl2) walks from setting up WSL2 to logging in to Claude Code / Codex, so Windows readers should open that first.

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

**Success**: the required `agentstack-doctor` checks show `ok:` with no `missing:` entries; inspect any `warn:` entries and their suggested fixes (informational `note:` and `mail-features:` lines are also normal), and `agentstack-selftest` ends with `self-test passed: two agents registered, exchanged messages, ...`. If either stops, go to the matching section of [Troubleshooting](troubleshooting.en.md).

### 3. Start the first agent and watch it on the dashboard

```bash
agent-start ~/code/my-project          # for Codex CLI: agent-start-codex ~/code/my-project
```

On macOS, open the dashboard from another terminal. On Windows, paste the same URL into your Windows browser.

```bash
open http://127.0.0.1:8770/
```

**Success**: your usual Claude Code or Codex starts, and one card with a scientist's name appears in the dashboard's DECK, showing the model and remaining context.

### 4. Spawn one child

Inside the running agent, ask for a child. This is the same in Claude Code and Codex.

```text
/delegate create a child agent and have it answer with its name and today's date
```

**Success**: a second card appears on the dashboard with a line from the parent to the child. When the child finishes, a "done" message arrives in the parent's terminal. The NETWORK tab shows the messages passing between the two.

By default the child's OS terminal opens automatically in the background. If you watch children from the dashboard, set `AGENTSTACK_AUTO_OPEN_CHILD=0` to stop the extra windows and open only the child you need with Deck Open tmux (see [Configuration](configuration.en.md#child-spawn)).

### 5. Play shiritori as an end-to-end check

The quickest way to confirm the whole install at once is a game of shiritori (Japanese word chain) between a Claude Code agent and a Codex child. Name registration, ORRERY Mail round trips, notification injection, and dashboard rendering all have to work for even one round to complete.

```text
/delegate spawn one Codex child and play shiritori with it. Use the installed ORRERY /delegate skill to create the child and ORRERY Mail (send_message) to communicate; pass these tool requirements to the child too. Do not use built-in Agent or SendMessage. Exchange one word per turn and report the result after ten round trips.
```

This standalone example uses ten round trips. [Workshop shiritori](https://github.com/gyroid-eth/orrery-workshop/blob/master/play/shiritori.md) and Full tour use three; the lecture uses that shorter version.

If you start from Codex, make the child a Claude Code agent. Either way, the point is to see the game go round between agents from two vendors.

**Success**: lines keep flowing back and forth between the two agents in NETWORK, and the "last instruction" on both DECK cards updates word by word. On the author's machine each turn took about five to six seconds per agent ([post with video](https://x.com/i/status/2095650715008168255), played at double speed).

Once you are here, just use your agents as usual. See [Installation](install.en.md) and [Configuration](configuration.en.md) for settings, and [Delegation and child agents](delegation.en.md) for how children work.


## Bundled skills and screen references

`/delegate` hands a job to a child and receives its completion report. See [Delegation and child agents](delegation.en.md) for the difference from native subagents, and [Launcher skills](launchers.en.md#skills-2-and-file-reservations) for registration, reservations, and startup. Claude discovers the standard skill path; Codex learns the same playbook location from managed `~/.codex/AGENTS.md` instructions.

`/log` records decisions, changes, verification, and next actions as Markdown in `logs/`. [Launcher log](launchers.en.md#log) and [Obsidian setup](obsidian.en.md) explain storage and Daily Note links. Those logs appear under Output on the dashboard.

[Dashboard](dashboard.en.md) contains detailed controls and images for DECK, NETWORK, DIGEST REPLAY, and NEW AGENT. For terminals and telemetry in one screen, start with the in-app guides in [ORRERY cockpit](https://github.com/gyroid-eth/orrery). [Using it with Obsidian](obsidian.en.md) includes a moving example and the public demo vault.

Screen examples: [DECK](img/deck.jpg), [NETWORK](img/network.jpg), and [NEW AGENT](img/new-agent.jpg).

## What runs underneath

- **ORRERY Mail**: one place for agent names, inboxes, and file reservations. It remains the source of truth even if the dashboard goes down.
- **Launchers**: `agent-start` registers the name, creates the tmux session, and starts the CLI in one step, so dashboard jumps and notification targets resolve to exactly one place.
- **Hooks**: Eight Claude event hooks stop unregistered sessions and unreserved writes, and inject arriving messages into the agent's prompt.

Details of these mechanisms and the API are in [Hooks](hooks.en.md), [Launchers](launchers.en.md), and the [API reference](api.en.md). Every dashboard view and control is available through the local HTTP API.


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


## Supported environments

| Environment | Support |
| --- | --- |
| macOS | Supported |
| Windows (WSL2) | Supported. Verified on Windows 11 + Ubuntu 26.04 / WSL 2.7 from install through spawning a child and jumping to a terminal from the dashboard. Steps: [WSL2 section of the install guide](install.en.md#installing-on-windows-wsl2) |
| Linux | Field reports received. On Ubuntu 24.04 / tmux 3.4, install, the dashboard, and Codex agents worked, and the two problems found there ([#26](https://github.com/gyroid-eth/orrery-telemetry/issues/26), [#27](https://github.com/gyroid-eth/orrery-telemetry/issues/27)) are fixed. The author has not verified `systemd --user` registration, so please report results as issues |
| Windows native (without WSL2) | The supported route is WSL2, but community contributions provide an experimental PowerShell helper that starts Mail and the dashboard, and a native Codex child launcher ([startup helper](windows-local.en.md), [Codex launcher](windows-codex-launcher.en.md)). Policy: [#3](https://github.com/gyroid-eth/orrery-telemetry/issues/3) |

Python 3.11 or newer, `git`, `tmux`, and `uv` are required, and at runtime at least one of Claude Code or Codex CLI. The installer checks these before writing anything and stops without changes if something is missing. The details of the checks and the optional dependencies (`fswatch`, `fzf`, Ghostty, Obsidian) are in the [install guide's environment section](install.en.md#supported-environment).


## Look before installing

The [public demo](https://agentstack-demo.pages.dev/) is a four-minute loop of the dashboard with scripted data and explanatory captions. It does not verify your own CLI, Mail, or delegation. The [“Orrery” introduction video](https://youtu.be/Jpc1ad7c90k) (about 90 seconds, English; [Japanese version](https://youtu.be/JXoa93TQolU) also available) illustrates the idea rather than recording the real screen.
