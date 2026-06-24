# claude-agent-stack

![claude-agent-stack demo](assets/demo.gif)

A coordination layer + live telemetry dashboard for running multiple
[Claude Code](https://docs.claude.com/en/docs/claude-code) (and Codex) agents on one machine.

It sits **on top of** [`mcp_agent_mail`](https://github.com/Dicklesworthstone/mcp_agent_mail)
(inter-agent messaging + file reservations) and adds:

- **Live dashboard** — see every tmux-resident agent, what it's working on, its
  context budget, parent/child spawn lineage, and agent-to-agent communication,
  updated in real time. Click an agent to jump to its terminal window.
- **Coordination hooks** for Claude Code — file-reservation guard, agent
  registration gate, child-agent spawning, and a mail watcher that injects
  inbox signals back into each agent's terminal.
- **One-command install** — `./install.sh` clones agent-mail, wires up the
  config, and starts the dashboard (macOS launchd / Linux systemd / plain nohup).
  The default Tier1 path shows a Claude Code user-settings diff and waits for
  an explicit `yes` before adding the small managed hook entries.


## Works best with Obsidian

This stack is usable with any project, but it **shines when your knowledge base
runs on [Obsidian](https://obsidian.md/)**. The coordination layer was designed
alongside an Obsidian vault, and the `/log` skill (phase 4) writes structured
work logs directly into your vault — complete with Daily Note backlinks, graph
connections, and tag-based maturity tracking.

Without Obsidian, logs fall back to a `logs/` directory in the current working
directory. All other features (dashboard, hooks, agent-mail) work regardless.

If you use Obsidian, set `AGENTSTACK_PROJECT_KEY` to your vault path and point
`AGENTSTACK_OBSIDIAN_APP` at the Obsidian CLI binary:

```bash
export AGENTSTACK_PROJECT_KEY="$HOME/path/to/your-vault"
export AGENTSTACK_OBSIDIAN_APP="/Applications/Obsidian.app/Contents/MacOS/obsidian"
```

## Requirements

- `python3`, `tmux`, `git`, and `uv` (for agent-mail)
- macOS or Linux (see [Windows / WSL2](#windows--wsl2) below)
- [Obsidian](https://obsidian.md/) *(recommended — unlocks full log integration)*
- [Ghostty](https://ghostty.org/) *(recommended — enables click-to-jump and auto window titles; falls back to iTerm2 → macOS Terminal → none automatically)*

## Quick start

```bash
git clone <this-repo> && cd claude-agent-stack
./install.sh
# open http://127.0.0.1:8770/
```

The dashboard binds to `127.0.0.1` by default. For access from a trusted LAN or
VPN, set `AGENTSTACK_BIND_HOST=0.0.0.0`; this exposes control endpoints and the
terminal bridge to that network.

## Windows / WSL2

Native Windows is not supported. On Windows, run everything inside
**WSL2** (Ubuntu 22.04+ recommended).

### 1. Install WSL2 and Ubuntu

```powershell
# Run in PowerShell (Admin)
wsl --install
# Restart when prompted, then open "Ubuntu" from the Start menu
```

### 2. Install dependencies inside WSL2

```bash
sudo apt update && sudo apt install -y python3 python3-pip tmux git curl
# uv (fast Python package manager, needed for agent-mail)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 3. Install Claude Code inside WSL2

```bash
npm install -g @anthropic-ai/claude-code
```

If `node` / `npm` is missing:

```bash
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs
```

### 4. Install claude-agent-stack

```bash
git clone <this-repo> && cd claude-agent-stack
./install.sh
```

The installer detects Linux and uses **systemd user units** (if available)
or a plain **nohup** fallback to keep the dashboard running.

### 5. Open the dashboard from Windows

The dashboard runs at `http://127.0.0.1:8770/` inside WSL2.
Open that URL in your **Windows browser** — WSL2 automatically forwards
localhost ports to Windows.

### Known limitations on WSL2

- **Ghostty is not available on WSL2.** The dashboard auto-detects this and
  sets `AGENTSTACK_TERMINAL=none`, which disables click-to-jump. You can still
  reach any agent manually with `tmux attach -t <name>` inside WSL2.
- Obsidian on Windows cannot directly read a vault that lives inside the WSL2
  filesystem (`\\wsl$\...`). Either keep your vault on the Windows filesystem
  and mount it in WSL2 (`/mnt/c/...`), or run Obsidian inside WSL2 with an
  X server / WSLg.

## Personal portrait overlays

The repository keeps bundled dashboard portraits generic. To use local portraits
without committing personal files, point the dashboard at a private PNG directory
and a private name-to-portrait JSON file:

```bash
export AGENTSTACK_PORTRAITS_DIR="$HOME/.agentstack/portraits_64"
export AGENTSTACK_CUSTOM_PORTRAITS="$HOME/.agentstack/custom_portraits.json"
```

Place `mybot.png` in the overlay directory, then map the lowercased registered
agent name to that portrait key:

```json
{"mybot":"mybot"}
```

See `examples/custom_portraits.example.json` for a neutral template. When these
environment variables are unset, the dashboard uses only the bundled portraits.

## License

Copyright (c) 2026 gyroid. All rights reserved.

This software is provided for personal use by authorized recipients only.
Redistribution, sublicensing, or commercial use without explicit written
permission from the author is prohibited.

`mcp_agent_mail` is a separate upstream dependency fetched at install time
and carries its own license.
