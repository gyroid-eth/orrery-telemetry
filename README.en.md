# ORRERY Telemetry

[日本語](README.md)

Claude Code, Codex, DeepSeek and Local LLM agents can talk directly, divide the work and check each other's results. ORRERY Telemetry shows their work and Mail round trips on one screen. Claude Code and Codex use their own CLIs; DeepSeek and Local LLMs use dsh or Hermes. Choose which to prepare: you do not need to install Claude Code or Codex before using DeepSeek or Local models. Pair Telemetry with [ORRERY cockpit](https://github.com/gyroid-eth/orrery) to use their terminals in the same workspace.

![A child joins the open NETWORK view, sends ready, and exchanges three shiritori Mail round trips](docs/img/network-mail.gif)

## Start from your goal

| Goal | Entry point |
| --- | --- |
| Observe agents on your machine | Start a configured agent and open DECK / NETWORK |
| Start with Claude Code or Codex | Install the shared components, sign in to your chosen CLI, then [start the first agent](docs/getting-started.en.md#3-start-the-first-agent-and-watch-it-on-the-dashboard) |
| Start with DeepSeek or Local LLMs | Install the shared components, then follow the [API setup guide](docs/native-setup.en.md) for dsh/Hermes, key storage, connection settings and launch. Claude/Codex login is not required |
| Have agents divide work | Follow the [delegate and Mail end-to-end check](docs/getting-started.en.md#5-play-shiritori-as-an-end-to-end-check) |

A provider supplies the connection, a model identifies what runs there, and a harness is the CLI that runs tools and the conversation. Standard API combinations currently cover DeepSeek / Local × dsh / Hermes, with availability checked per configured model. This does not promise arbitrary API and harness combinations.

Once configured, DeepSeek / Local models appear under Model and Harness (dsh / Hermes) in NEW AGENT. When adding one for the first time, install the application, store your API key and connect the settings using the [setup guide](docs/native-setup.en.md). Keys belong in Mac Keychain or Ubuntu Secret Service, not in NEW AGENT. API launching currently requires legacy mode. Hermes keeps each agent's conversations and memory separate by project and registered name. There is no universal provider login or shared Hermes Telegram entry point yet, and manual Local effort still needs real-server verification. See the [supported scope and limitations](docs/managed-catalog.en.md#current-limitations) and the [selection screen examples](docs/dashboard.en.md#native-models-and-harnesses).

![Select a configured Local model and dsh / Hermes in NEW AGENT](docs/img/native-harness-picker.png)

The actual bundled Telemetry NEW AGENT, showing the real scientist names, portraits, and Claude / Codex / DeepSeek / Local models choices. [Watch the selection GIF](docs/img/native-harness-selection.gif); it stops before launching. The [cockpit NEW AGENT](https://github.com/gyroid-eth/orrery/blob/master/docs/en/usage.md) is a separate screen.

## Install Telemetry only

This repository alone gives you the part that runs behind the scenes. You need Python 3.11 or later, `git`, `tmux`, and `uv` (on a Mac: `brew install tmux uv`). On Windows, run it inside WSL2 Ubuntu.

```bash
git clone https://github.com/gyroid-eth/orrery-telemetry.git
cd orrery-telemetry
./scripts/install.sh --project-key /absolute/path/to/your-project
```

`--project-key` is the absolute path of the folder where agents work, not this repository. Review and confirm the changes, then check the shared components with `~/.agentstack/bin/agentstack-doctor` and `~/.agentstack/bin/agentstack-selftest`. Continue with your chosen provider:

- **Claude Code / Codex**: Install and sign in to the chosen CLI in the same OS environment. Start it with `~/.agentstack/bin/agent-start <project folder>` or `~/.agentstack/bin/agent-start-codex <project folder>`.
- **DeepSeek / Local LLMs**: Follow the [API setup guide](docs/native-setup.en.md) to prepare dsh or Hermes, key storage and connection settings. Then select and launch it from NEW AGENT at `http://127.0.0.1:8770/`. The install command above does not complete this API setup.

For either route, check that the agent appears in DECK. The [standalone procedure](docs/getting-started.en.md#standalone-installation-and-verification) covers preparation, launch and Mail verification.

Installer `--child-default-tools` selects default child tools (empty by default). Explicit choices win; unavailable default items are omitted with a notice. See [tool selection](docs/delegation.en.md#installer-defaults).

Pass browser or computer use tools to a child with `--tools`. See [reachable tools and what a person does](docs/delegation.en.md#browser-and-computer-use-what-is-reachable-and-what-a-person-does).

At a work boundary, EXIT a child and change its tools in the same conversation with `~/.agentstack/bin/agentstack-resume <NAME> --tools <spec>`. See [changing tools on resume](docs/delegation.en.md#resume-a-stopped-child-with-different-tools).

**What works standalone**: the dashboard's Telemetry screen (DECK, NETWORK, REPLAY, RESUME of retired agents, NEW AGENT), starting agents with `agent-start` or `agent-start-codex`, creating children with the delegate skill (Claude Code: `/delegate`, Codex: `$delegate`), and ORRERY Mail between agents. Agent terminals open in your OS terminal (a Windows Terminal tab on Windows).

**What is not included**: the cockpit screen that arranges and drives terminals in the browser (composer, Split, Planetarium, `ORRERY.app`).

## How it relates to cockpit

![The cockpit is the screen for operating terminals; Telemetry is the dashboard and agent-graph screen. The cockpit can embed the Telemetry screen. Agents run in tmux, are visible from both screens, and talk through Mail](https://raw.githubusercontent.com/gyroid-eth/orrery/master/docs/images/cockpit_telemetry_relation.svg)

- **Telemetry** is what runs agents, Mail, and the dashboard behind the scenes, plus the Telemetry screen that shows their state.
- **[Cockpit](https://github.com/gyroid-eth/orrery/blob/master/README.en.md)** is the screen where you arrange and drive terminals. Starting agents, Mail, and the Telemetry screen are handled by Telemetry, so cockpit is used together with Telemetry (its terminals open even when Telemetry is not running).
- Installing from the cockpit side **installs both.**

## To install both (the orrery one-liner)

Run this in a Mac terminal, or inside WSL2 Ubuntu. It installs cockpit and Telemetry together.

```bash
curl -fsSL https://raw.githubusercontent.com/gyroid-eth/orrery/master/scripts/get.sh | bash
```

Where things go (`~/orrery`, `~/orrery-telemetry`, `~/.agentstack`, `~/orrery-work`), the ports, how to change the work folder, and how to remove everything are in [orrery's README](https://github.com/gyroid-eth/orrery/blob/master/README.en.md#quick-start). To remove a standalone Telemetry, see [Uninstall](docs/install.en.md#uninstall).

## Start with the guides on screen

Try **Your first flight → help map → Full tour** in cockpit. Learn the basics with the seven-item guide on the right on your first visit; look around with `Settings → Getting started → Show help map`; then choose `Full tour` there for sixteen steps in order.

In the Telemetry header, choose `HELP MAP` (beside `SETTINGS` in NETWORK) to annotate the visible DECK / NETWORK controls. In an agent detail panel, choose the `HELP MAP` button in its action row to annotate the panel’s controls. These entries also work when Telemetry is embedded in cockpit. If the annotations do not fit, they appear as a legend; press `Esc` or choose `Close` to dismiss it.

Full tour guides you through creating one child with the delegate skill (Claude Code: `/delegate`, Codex: `$delegate`), playing three shiritori round trips over ORRERY Mail, arranging terminals, and trying Telemetry EXIT / RESUME and NETWORK / REPLAY. Follow the controls described on screen to advance. DeepSeek and Local children can use the same manual three-round-trip check; verify dsh and Hermes separately ([steps](docs/getting-started.en.md#5-play-shiritori-as-an-end-to-end-check)). See the [current quick start](https://github.com/gyroid-eth/orrery/blob/master/README.en.md#quick-start) and [Full tour](https://github.com/gyroid-eth/orrery/blob/master/docs/en/FULL_TOUR.md).

Cockpit is where you work in terminals; Telemetry is where you view the team's status and history. Start with the in-app guides, then use the [documentation index](docs/README.en.md) for detailed references.

## Resume a retired agent

In Telemetry, choose DECK history `7D` / `30D` / `ALL`, or NETWORK's `ALL` range, to find exited agents. Open an agent that can resume and choose **RESUME** in its detail panel to return to the same work. When history, credentials, or the original working directory cannot be verified, the UI explains why and stops the resume.

Standalone Telemetry example: filter by name, find the `RETIRED` / `RESUME READY` agent in `7D`, open it, and choose `RESUME`. After the notification confirms the conversation resumed in tmux, switch to `LIVE` and check that the agent is `ONLINE`.

![Resume a retired agent from 7D in standalone Telemetry and confirm it returns ONLINE in LIVE](docs/img/resume-retired-agent.gif)

[Dashboard search and resume](docs/dashboard.en.md#search) and [Launcher Claude resume](docs/launchers.en.md#dashboard-claude-resume) cover cases that need verification and cases that cannot resume.
For global network activity, owner verification and unknown boundaries, see the [observation guide](docs/dashboard.en.md#global-runtime-observation).

## Troubleshoot and update

- Not working: run `~/.agentstack/bin/agentstack-doctor` for missing pieces and `~/.agentstack/bin/agentstack-selftest` for Mail round trips, then use [Troubleshooting](docs/troubleshooting.en.md).
- Update: use the orrery one-liner with cockpit, or [Upgrade](docs/install.en.md#upgrade) for standalone Telemetry. See [Uninstall](docs/install.en.md#uninstall) to remove it.
- Windows: [WSL2 setup and sign-in](docs/install.en.md#installing-on-windows-wsl2). Native Windows helpers are a [separate experimental route](docs/windows-local.en.md).
- Environments, terms, and standalone startup: [Getting started](docs/getting-started.en.md).

## Documentation

Japanese documents are the source of truth. The English entry point is the [English index](docs/README.en.md).

| What you want to read | Entry point |
| --- | --- |
| Find material by purpose | [All docs](docs/README.en.md) |
| Start Telemetry on its own or look up a term | [Getting started](docs/getting-started.en.md) |
| Check installation, updates, and requirements | [Installation](docs/install.en.md) |
| Read from one designated phone | [Mobile view-only setup](docs/mobile.en.md) (off by default) |
| Diagnose a problem | [Troubleshooting](docs/troubleshooting.en.md) |
| Develop a local deterministic daemon helper | [Dedicated bindings and notifications](docs/daemon-agents.en.md) (deployment requires separate approval) |
| Validate candidate absolute reservations | [Candidate contract and tests](docs/absolute-reservations.en.md) (legacy mode remains the default) |
| Validate candidate Mail and delivery migration | [State migration and recovery](docs/namespace-state-migration.en.md) (activation false) |
| Validate isolated global server S1 / S2a | [Wire contract and capabilities](docs/global-server.en.md) (legacy mode remains default) |
| [Global child client](docs/global-child-client.en.md) | Prepare child registration, resume and hooks with fixed roots |
| Validate isolated global reservations/welcome | [S2c contract and recovery](docs/global-reservations.en.md) (legacy remains default) |
| Validate isolated messages and signals | [S2b DB and notification contract](docs/global-messages.en.md) (legacy mode remains default) |
| Validate isolated runtime clients | [PR4a registration, recovery and await](docs/global-runtime-client.en.md) (legacy mode remains default) |
| Launch isolated global clients and deliver notifications | [Launch, delivery and operator recovery](docs/global-delivery-client.en.md) (legacy remains default) |
| Move instructions to the user scope | [Block migration and recovery](docs/instruction-migration.en.md) |

[Using it with Obsidian](docs/obsidian.en.md) and the public [demo vault](https://github.com/gyroid-eth/orrery-demo-vault) turn logs, paper notes, and tasks into notes; Obsidian is optional. [Codex App](docs/codex-app.en.md) and [Antigravity / Gemini](docs/antigravity.en.md) are optional providers beyond the CLIs.

Look before installing with the [public demo](https://agentstack-demo.pages.dev/); the [introduction video](https://youtu.be/Jpc1ad7c90k) illustrates the idea ([Japanese version](https://youtu.be/JXoa93TQolU)). See [CONTRIBUTING.md](CONTRIBUTING.md) before contributing code.

[Reusable Codex plugin lifecycle and isolated verification (Japanese)](docs/codex-plugin-lifecycle.md).

## License

This repository uses the **PolyForm Perimeter License 1.0.1**. It is source-available, not open source in the OSI sense. See [LICENSE](LICENSE) for the complete terms.

- You may use, modify, and redistribute it for any purpose.
- You may **not** provide others with a product that competes with this software. Competing covers free distribution, ports to another language, and delivery as a service, library, or plug-in.

Attribution for inherited or derived portions of the bundled service is recorded in its [NOTICE](packages/agentstack_mail/NOTICE.md), and their license is preserved as [UPSTREAM_LICENSE](packages/agentstack_mail/UPSTREAM_LICENSE). Portions written for ORRERY Telemetry ("AgentStack" in the file name is the former project name) use [AGENTSTACK_LICENSE](packages/agentstack_mail/AGENTSTACK_LICENSE). See [Third-party components](docs/third-party.md) for the boundary.

The scientist portraits on the dashboard (`dashboard/portraits_pixel/` and its 64 px reductions in `dashboard/portraits_64/`) are pixel art generated by the author (gyroid) with ChatGPT image generation from text prompts only; no photographs were used as input. They are distributed under the same terms as the repository. Their provenance is recorded in [Third-party components](docs/third-party.md#肖像画像).

For isolated global dashboard replies and instruction notices, see the [dashboard guide](docs/dashboard.en.md#global-mail-view). Legacy mode remains the default.
