# claude-agent-stack

[日本語](README.md)

A coordination layer and live telemetry dashboard for local Claude Code and Codex agents. It uses [mcp-agent-mail](https://github.com/Dicklesworthstone/mcp_agent_mail) as the source of truth for messages, identities, and file reservations, then overlays tmux runtime state, spawn lineage, communication history, and remaining context in one UI.

![claude-agent-stack demo](assets/demo.gif)

The core idea is to make operating rules executable through launchers, hooks, mail, and observability instead of merely asking an LLM to coordinate well.

## Features

- `agent-start` and `agent-start-codex` create tmux sessions named after their agent-mail identities
- Claude Code hooks enforce registration and file reservations and record session state
- An agent-mail watcher injects inbox signals into active Claude and Codex REPLs
- DECK and NETWORK views show status, ownership, communication, spawn lineage, context, and outputs
- The dashboard controls graceful EXIT, RESUME, REPLAY, role annotations, and child spawning
- Bundled `/delegate` and `/log` skills plus managed instructions for Codex and Claude
- Optional Obsidian integration for logs, Daily Note backlinks, and output indexing

The stack extends rather than replaces agent-mail. Keeping mail and reservations in one authoritative system prevents coordination state from splitting when the UI restarts.

## Requirements

- macOS is the primary target. Launchers and hooks support the system Bash 3.2
- `python3`, `tmux`, `git`, and `uv`
- Claude Code or Codex CLI
- `fzf` (optional directory picker)
- Ghostty (recommended for click-to-jump and titles; falls back to iTerm2, Terminal.app, or `none`)
- Obsidian (optional vault integration)

Linux uses a systemd user service when available and otherwise falls back to `nohup`. Native Windows is unsupported. WSL2 can serve the localhost dashboard, but Ghostty click-to-jump is unavailable.

## Installation

```bash
git clone https://github.com/gyroid-eth/claude-agent-stack.git
cd claude-agent-stack
./scripts/install.sh
```

Open `http://127.0.0.1:8770/`, then run:

```bash
~/.agentstack/bin/agentstack-doctor
```

The installer fetches upstream agent-mail into `~/mcp_agent_mail` and installs the dashboard, launchers, hooks, skills, managed-instruction templates, and `VERSION` under `~/.agentstack`. Generated `env.sh` is mode `0600` and contains no bearer token.

### Install tiers

| Command | Tier | Behavior |
| --- | --- | --- |
| `./scripts/install.sh` | Tier 1 / default | Installs all payloads. Hooks, permissions, `skillsDirectories`, and managed Codex / Claude blocks are merged only after a preview and explicit `yes` |
| `./scripts/install.sh --dashboard-only` | Tier 0 | Installs dashboard and helpers, but not hooks, skills, or instruction templates |
| `./scripts/install.sh --scoped` | Tier 2 placeholder | Installs payloads without changing user settings or managed docs |
| `./scripts/install.sh --dry-run` | Preview | Prints planned changes without writing files or services |

The JSON-based merge preserves existing settings, records added hooks, permissions, and skill directories in the manifest, and creates a backup. This makes reruns and uninstall auditable.

Core options:

```text
--install-dir PATH      default: ~/.agentstack
--project-key PATH      default: AGENTSTACK_PROJECT_KEY, PROJECT_KEY, repo root
--port PORT             default: 8770
--label-prefix PREFIX   default: org.agentstack
--terminal MODE         auto | ghostty | iterm | terminal | none
```

`--bin-dir` is not a public installer option. The installer passes `agentstack-merge-settings --bin-dir ~/.agentstack/bin` internally to expand `__AGENTSTACK_BIN_DIR__` safely in permission rules.

The repository `VERSION` is authoritative. It is copied into the install root; `GET /api/version` checks the installed artifact, repository artifact, then Git metadata.

### macOS TCC and Full Disk Access

`~/Desktop`, `~/Documents`, and `~/Downloads` are protected by macOS TCC. If the root agent starts from a terminal without Full Disk Access, that app identity propagates through tmux to descendants and can cause child-only `EPERM` failures.

- Start the root agent from a terminal with Full Disk Access
- Or move the project outside protected folders
- Recreate existing tmux servers and sessions after changing access context

The launcher warns about this condition. Use `AGENTSTACK_TCC_GUARD=0` to silence it or `AGENTSTACK_TCC_DIRS` to change the protected roots. `chmod` alone cannot fix an app-identity decision.

## Launching agents

```bash
export PATH="$HOME/.agentstack/bin:$PATH"

agent-start ~/code/my-project
agent-start-codex ~/code/my-project
```

With no argument, the launcher uses an `fzf` picker rooted at `AGENTSTACK_BASE_DIR` (default `$HOME`) or falls back to the current directory.

```bash
export AGENTSTACK_BASE_DIR="$HOME/Obsidian/MyVault"
agent-start
```

Outside tmux it creates a named session; inside tmux it renames the current session and starts in place. Matching the tmux name to the agent-mail identity makes jump, signal delivery, and token recovery unambiguous.

### Scientist names and fail-closed checks

New identities use `Adjective-Scientist`, such as `Swift-Bohr`. Adjectives come from the bundled list and scientist suffixes from `dashboard/scientist_portraits.json`. The suffix is also the portrait key.

Name availability is three-valued: `available`, `occupied`, or `unknown`. Transport failures, authentication errors, and timeouts are `unknown`, never “free.” Three consecutive unknown results stop selection by default, preventing an unverifiable candidate from claiming a live identity.

### Identity and tokens

Re-registering an identity requires its original `registration_token`. Top-level tokens are stored mode `0600` at:

```text
${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}/agent_token_<name>
```

Delegated children also have child-owned state under `child-agents/<name>.json`; a parent token is not forwarded to a pre-registered child. The `CHILD_REGISTRATION_TOKEN` name is historical and also covers top-level reauthentication.

```bash
AGENTSTACK_PROJECT_KEY=/path/to/project \
  ~/.agentstack/bin/agentstack-reregister "$AGENT_NAME"
```

The helper restores the token without printing it in the transcript or a process argument. Do not create a new alias when same-name registration fails: that breaks inbox and thread continuity.

Launchers set `CLAUDECODE=1` in each tmux session to guard against interactive-shell exit hooks that could cascade-kill the tmux server. They also clear inherited `AGENT_NAME`, `PARENT_AGENT`, tokens, and reserved markers before a top-level launch to prevent identity hijacking.

Codex has no Claude Code hook system, so `agentstack-codex-bootstrap` handles pre-launch registration and tmux renaming. `OPENAI_API_KEY` is removed so Codex uses ChatGPT OAuth.

## Dashboard

<!-- TODO: screenshot: DECK view -->

### DECK

DECK is an operational card view:

- Sections for running, standby, finished, gone, and retired agents
- Portrait, model, provider, context window, task, last instruction, outputs, and attach state
- Work, wait, approval, question, and elapsed-time indicators
- A green, amber, or red context-remaining hairline along the card edge
- Live pane title, agent-mail activity, filtering, and `show all`
- History / Output panel, tmux open, and two-step EXIT for running agents
- Two-step KILL / soft retire for unattached finished or gone agents

The server reuses `build_agents()` category state when authorizing KILL instead of trusting the frontend. A single classification source avoids deleting a session after inconsistent running detection.

### NETWORK

<!-- TODO: screenshot: NETWORK view -->

NETWORK overlays spawn lineage and agent-mail communication on a force graph:

- Portrait medallions, provider badges, running halo, and state motion rings
- A static context-remaining arc up to 270 degrees around each node
- Hover / long-press tooltip with task, live state, model, and recent activity
- Node detail panel: transcript plus 24-hour History sparkline, and vault `LOG_*.md` Output
- ROLE ASSIGN for role / group annotation
- Clickable communication edges opening a mail drawer with subject, importance, time, and body
- Time window / ALL, mail comets, spawn edges, and legend
- TUNE controls for size, distance, width, repulsion, centering, and spring, persisted in localStorage
- Dense mode above 300 nodes, hiding labels, annotations, badges, and context arcs

Without `AGENTSTACK_PROJECT_KEY` or `AGENTSTACK_VAULT`, the edge drawer shows `NOT CONFIGURED`. Local tmux telemetry remains available so mail configuration failures do not look like a dead dashboard.

### SELECT and bulk actions

SELECT mode supports rectangle drag or node clicks:

- EXIT sends `/api/exit` to running / finished agents
- RESUME sends `/api/jump` to gone / retired agents; missing tmux sessions fall back to transcript resume
- REPLAY starts DIGEST REPLAY for at least two agents with mail history

EXIT and RESUME require two-step confirmation and dispatch sequentially at 60 ms intervals to reduce accidental actions and service spikes.

### DIGEST REPLAY

REPLAY plays selected-agent mail, spawn, exit / retire, and approval events over time.

- Play / pause, seek, event markers, and absolute / relative clock
- Logarithmic speed from `×1` to `×10000`
- Message HOLD from `0.1s` to `15s`
- GROUP-ONLY filtering
- TIME-TRAVEL rebuilds nodes, edges, and states from the initial snapshot; OFF replays comets on the current graph
- `Esc` / CLOSE restores the live graph snapshot and mail polling

The range auto-fits the oldest and newest events, expands very short spans for usability, and preserves topology and in-group mail when large histories are sampled.

### NEW AGENT

<!-- TODO: screenshot: NEW AGENT modal -->

`+ NEW AGENT` uses the `/api/spawn-names` catalog to select a scientist, adjective, working directory, model, parent, role / group, task, and optional isolated worktree. Occupied or unknown scientists are disabled. Selecting one adds a shuffled adjective and composes a separator-free name such as `WindyCurie`; leaving it unselected omits `name` and delegates naming to agent-mail.

Spawning runs `register_agent → annotate → send_message → spawn_child.sh --pre-registered`. It passes the token through a mode-`0600` file and verifies the tmux session after three seconds. Failures include the tail of `dashboard/logs/spawn.log`.

### Embed mode

`/?embed=1` and same-origin iframes use a compact embed layout. The parent may send:

```js
frame.contentWindow.postMessage({type: "net-pause"}, location.origin);
frame.contentWindow.postMessage({type: "net-resume"}, location.origin);
```

Pause stops polling; resume immediately refreshes the current view. This prevents hidden iframes from continuously polling tmux and SQLite.

## API reference

The dashboard binds to `127.0.0.1` by default and has no authentication layer. Setting `AGENTSTACK_BIND_HOST=0.0.0.0` exposes control endpoints and the terminal bridge to the trusted LAN or VPN.

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/version` | Name, version, API revision |
| GET | `/api/spawn-names` | Name status, adjectives, directories, and models |
| GET | `/api/agents` | tmux + mail rows for DECK |
| GET | `/api/graph?days=4&all=0` | NETWORK nodes, communication edges, spawn edges |
| GET | `/api/history?session=NAME&limit=220` | Claude / Codex transcript |
| GET | `/api/agent-history?name=NAME&hours=24` | One-agent events; use `names=A,B` for replay and `include_pane_states=1` for approval events |
| GET | `/api/edge-messages?a=A&b=B&limit=60` | Two-agent mail drawer |
| GET | `/api/messages-since?since=EPOCH&limit=80` | Live mail comets |
| GET | `/api/annotations` | Role / emoji / group map |
| GET | `/api/deliverables?agent=NAME` | Vault output index |
| GET | `/api/custom-portraits` | Custom portrait map |
| GET | `/api/term?session=NAME&lines=500` | tmux capture |
| GET | `/api/ptty?session=NAME` | Ensure a ttyd browser terminal |
| GET | `/api/mail-watcher-health` | Watcher, signal backlog, recent delivery results |
| POST | `/api/jump` | Open / focus tmux or resume from transcript |
| POST | `/api/exit` | Graceful `/exit` |
| POST | `/api/kill` | Kill tmux and soft-retire finished / gone agents |
| POST | `/api/annotate` | Upsert or remove role annotations |
| POST | `/api/spawn` | Register, message, and launch a child |

Static resources include `/portrait?name=Curie&hi=1` and `/assets/*.svg|png`.

### Request and response examples

```bash
curl -s http://127.0.0.1:8770/api/version
```

```json
{"name":"claude-agent-stack","version":"0.9.0","api":1}
```

```bash
curl -s http://127.0.0.1:8770/api/spawn-names
```

```json
{
  "names":[{"name":"Curie","portrait":true,"status":"available"}],
  "adjectives":["Windy","Curious"],
  "naming":"adjective+scientist",
  "dirs":["~","/path/to/project"],
  "models":["claude-sonnet-5","claude-opus-5","claude-haiku-4-5-20251001"],
  "default_model":"claude-sonnet-5"
}
```

```bash
curl -s -X POST http://127.0.0.1:8770/api/annotate \
  -H 'Content-Type: application/json' \
  -d '{"name":"WindyCurie","role":"docs","group":"release"}'
```

```json
{"ok":true,"annot":{"name":"WindyCurie","role":"docs","emoji":"","group":"release"}}
```

Empty `role` and `emoji` remove the annotation.

```bash
curl -s -X POST http://127.0.0.1:8770/api/spawn \
  -H 'Content-Type: application/json' \
  -d '{
    "parent":"Curious-Copernicus",
    "name":"WindyCurie",
    "dir":"/path/to/project",
    "model":"claude-sonnet-5",
    "role":"docs",
    "group":"release",
    "task":"Verify the README",
    "worktree":false
  }'
```

```json
{"ok":true,"child_name":"WindyCurie","tmux_session":"WindyCurie","annot":"ok","worktree":false}
```

`name` is optional and must be confirmed `available` when present. `parent`, `task`, an existing `dir`, and an allowed model are required.

```bash
curl -s -X POST http://127.0.0.1:8770/api/exit \
  -H 'Content-Type: application/json' \
  -d '{"session":"WindyCurie"}'
```

```json
{"ok":true,"session":"WindyCurie","actions":["exit-sent"]}
```

Errors normally use `{"ok":false,"error":"..."}` with HTTP 400. An unavailable spawn catalog source returns HTTP 503.

## Configuration

### Dashboard / `server.py`

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTSTACK_PORT` | `8770` | HTTP port |
| `AGENTSTACK_BIND_HOST` | `127.0.0.1` | Bind address |
| `AGENTSTACK_MAIL_DB` | `~/mcp_agent_mail/storage.sqlite3` | agent-mail SQLite |
| `AGENTSTACK_MAIL_ENV` | `~/mcp_agent_mail/.env` | Bearer-token file used by dashboard spawn |
| `AGENTSTACK_PROJECT_KEY` | unset | agent-mail project human key |
| `AGENTSTACK_VAULT` | unset | Project-key fallback and Output scan root |
| `AGENTSTACK_LABEL_PREFIX` | `org.agentstack` | launchd label prefix |
| `AGENTSTACK_TERMINAL` | `auto` | `ghostty`, `iterm`, `terminal`, or `none` |
| `AGENTSTACK_HOOKS_DIR` | `~/.agentstack/hooks` | Hook and default spawn-script root |
| `AGENTSTACK_RUNTIME_DIR` | `~/.claude/runtime` | Tokens and notification runtime state |
| `AGENTSTACK_MAIL_HOME` | `~/.mcp_agent_mail` | Signal-data root |
| `AGENTSTACK_SIGNALS_DIR` | `$AGENTSTACK_MAIL_HOME/signals` | Mail signal root |
| `AGENTSTACK_PORTRAITS_DIR` | unset | Private PNG overlay |
| `AGENTSTACK_CUSTOM_PORTRAITS` | unset | Agent-name to portrait-key JSON |
| `AGENTSTACK_SPAWN_SCRIPT` | `$AGENTSTACK_HOOKS_DIR/spawn_child.sh` | NEW AGENT launcher |
| `AGENTSTACK_SPAWN_DIRS` | `~` | Colon-separated NEW AGENT presets |

With both `AGENTSTACK_PROJECT_KEY` and `AGENTSTACK_VAULT` unset, DECK tmux state, terminal open, and local annotations still work. Shell-side agent registration, edge mail, history / replay, dashboard spawn, project-scoped retire, and vault Output do not.

### Installer / launchers

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTSTACK_HOME` | `~/.agentstack` | Install root |
| `AGENTSTACK_MAIL_DIR` | `~/mcp_agent_mail` | Upstream clone |
| `AGENTSTACK_AGENT_MAIL_REPO` | Upstream GitHub URL | Clone source |
| `AGENTSTACK_MCP_URL` | `http://127.0.0.1:8765/mcp` | agent-mail MCP endpoint |
| `AGENTSTACK_BASE_DIR` | `$HOME` | Launcher picker root |
| `AGENTSTACK_CLAUDE_BIN` | `claude` | Claude CLI |
| `AGENTSTACK_CODEX_BIN` | `codex` | Codex CLI |
| `AGENTSTACK_CODEX_SANDBOX` | `workspace-write` | Codex sandbox |
| `AGENTSTACK_CODEX_APPROVAL` | `on-request` | Codex approval mode |
| `AGENTSTACK_VAULT` | unset | Extra Codex `--add-dir` writable directory |
| `AGENTSTACK_PROTECTED_ROOTS` | project key | Roots protected by the Claude reservation hook |
| `AGENTSTACK_CONTACT_POLICY` | `open` | Post-registration contact policy; `skip` keeps the server default |

Generated `~/.agentstack/env.sh` is the normal configuration point. Service environments are rendered into the plist / unit at install time, so rerun the installer or update the service definition after changes.

`AGENTSTACK_MCP_URL` configures launcher and hook connections. The current dashboard `/api/spawn` path uses the fixed `http://127.0.0.1:8765/mcp`, so verify that path as well when using another endpoint.

## Customization

### Portrait overlays

```bash
export AGENTSTACK_PORTRAITS_DIR="$HOME/.agentstack/portraits_64"
export AGENTSTACK_CUSTOM_PORTRAITS="$HOME/.agentstack/custom_portraits.json"
```

Put `mybot.png` in the overlay and map lowercased registered names to portrait stems:

```json
{"mybot":"mybot","windycurie":"Curie"}
```

See `examples/custom_portraits.example.json`. Private assets stay separate from generic distribution portraits.

### NEW AGENT directory presets

```bash
export AGENTSTACK_SPAWN_DIRS="$HOME/code:$HOME/Obsidian/MyVault:/tmp"
```

The API keeps `~` symbolic and expands it only at spawn time. The UI remembers the last selection in localStorage.

## Skills and file reservations

The installer places `skills/delegate` and `skills/log` in `~/.agentstack/skills`.

- `/delegate` declares and reserves resources, launches and supervises a Claude or Codex child, and supports model and worktree selection
- `/log` writes a vault-connected log with Daily Note links, or falls back to local `logs/`

Claude Code hard-blocks unreserved `Edit` / `Write` operations through `check-file-reservation.sh`. Codex has no equivalent hook, so managed `~/.codex/AGENTS.md` supplies the reserve / renew / release discipline. Both use the same registry.

## agent-mail is a separate component

`mcp_agent_mail` is not bundled. The installer fetches upstream, verifies an existing clone's remote, and retains its clone, data directory, database, and `.env` on uninstall by default.

Upstream uses the **MIT License with OpenAI/Anthropic Rider**. It adds restrictions covering OpenAI, Anthropic, and related parties. Read the fetched `~/mcp_agent_mail/LICENSE` for exact terms. This repository's own authoritative terms are in [LICENSE](LICENSE); the two components are licensed separately.

## Troubleshooting

### `NOT CONFIGURED`

The dashboard service has neither `AGENTSTACK_PROJECT_KEY` nor `AGENTSTACK_VAULT`. Check both `~/.agentstack/env.sh` and the launchd plist / systemd unit, rerun the installer, and restart the service.

### launchd does not start

```bash
label=org.agentstack.agentdashboard
plist="$HOME/Library/LaunchAgents/$label.plist"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl enable "gui/$(id -u)/$label"
tail -f ~/.agentstack/dashboard/dashboard.log
```

The installer stops if the port is occupied; change `AGENTSTACK_PORT` or `--port`.

### Notification text appears in Codex but is not submitted

Keep text and submit in separate tmux calls:

```bash
tmux send-keys -t "$session" -l "$text"
sleep 0.2
tmux send-keys -t "$session" C-m
```

Some Codex REPLs do not submit the `Enter` keysym reliably, so the watcher uses `C-m`. It avoids bare shells and runs tmux calls in timeout-bound workers.

### A dashboard-spawned agent dies immediately

1. Read the tail of `dashboard/logs/spawn.log`
2. Run `tmux has-session -t '<child-name>'`
3. Verify `~/.local/bin/claude` or the selected CLI is on the service `PATH`
4. Check `AGENTSTACK_SPAWN_SCRIPT`, the working directory, and `AGENTSTACK_PROJECT_KEY`
5. Check token state with `agentstack-reregister '<child-name>'`

The dashboard probes the session after three seconds. Because launchd often omits `~/.local/bin`, the spawn path prepends it.

### Registration or inbox authentication fails

Do not create an alias. Run `agentstack-reregister "$AGENT_NAME"` and check `agent_token_<name>` or `child-agents/<name>.json`. Report missing, stale, or wrong-owner token state to the parent or operator.

### tmux scrollback does not work

```tmux
set -g mouse on
set -g history-limit 50000
```

Or enter copy mode with `Ctrl+b [`. `agentstack-doctor` also checks mouse mode.

## Upgrade / uninstall

```bash
git pull
./scripts/install.sh
~/.agentstack/bin/agentstack-doctor
```

The installer refreshes payloads and `VERSION`, re-registers the service, and previews managed merges again. Managed blocks are idempotent.

```bash
~/.agentstack/bin/agentstack-uninstall
```

The agent-mail clone, mail home, database, and `.env` are retained by default. `~/.agentstack/install-state.json` is authoritative for removal scope.

## License

This repository is licensed under the **MIT License (with OpenAI/Anthropic Rider)** — a standard MIT grant plus a rider restricting provision to OpenAI, Anthropic, and related parties. See [LICENSE](LICENSE) for the full text.

`mcp_agent_mail` is a separate component under the MIT License with OpenAI/Anthropic Rider described above.
