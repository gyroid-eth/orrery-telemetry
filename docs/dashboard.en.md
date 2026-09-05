# Dashboard

> 日本語版: [dashboard.md](dashboard.md)

[Previous: Codex App integration](codex-app.en.md) · [Back to README](../README.en.md) · [Next: API reference](api.en.md)

The dashboard is served at `http://127.0.0.1:8770/` by default. It combines tmux, agent-mail SQLite, runtime state, project logs, and optional Obsidian-link hints into one screen for observation and safe control operations.

Here, agent-mail / mail watcher means the mechanism for messages among agents inside AgentStack. It never accesses the user's email account, mail client, or inbox.

## Find an action

| Goal | Shortest path |
| --- | --- |
| See agents currently running | Open [DECK](#deck) and inspect the cards under `ACTIVE AGENTS`. Click a card to see its task, live state, History, Output, and terminal actions together. |
| See parent-child relationships | Switch to [NETWORK](#network). Spawn edges connect parents and children; click a node for its detail panel. |
| Read what agents said to each other | Click a communication edge in NETWORK. The right-side mail drawer displays subject, importance, time, and body between those two agents. Without [mail configuration](#edges-and-mail), it shows `NOT CONFIGURED`. |
| Operate several agents together | Enable `Select` above NETWORK, then click nodes or drag a rectangle over empty space. The bottom action bar offers `Exit N` for running / finished agents and `Replay N` for two or more. EXIT requires pressing the same button twice. |
| Resume a finished agent | For tmux-based Claude / Codex CLI agents, show past agents with DECK `show all` or NETWORK `ALL`, then choose card / node → detail panel → `OPEN TMUX`. If the tmux session is absent, `/api/jump` switches to saved-transcript resume. Another path selects gone / retired nodes in NETWORK and presses bottom-bar `Resume N` twice. Resume requires the transcript, original cwd, corresponding CLI, and terminal adapter. |
| See finished agents | Set NETWORK's time window to `ALL` or enable DECK `show all`. DECK shows `gone` / `retired` cards from the last 30 days. See also [After a child completes](#after-a-child-completes). |

NETWORK may omit nodes outside its selected time window. Absence from the current graph alone does not mean task failure; check `ALL`, DECK `show all`, and the completion report delivered to the parent.

## DECK

DECK opens first. **One card represents one agent**, so you can inspect who is doing what and whether each agent is stopped waiting for input.

In the following image, the top counter and card area both show three agents. The counter is the total; each card shows an individual state.

![Initial DECK state, with three agents shown as one card each](images/deck_start.png)

The six screenshots on this page are real screens from an isolated demo environment. Every agent name, task, mail message, and transcript is fictional and does not represent real work.

### Reading a card from top to bottom

| Display | Interpretation |
| --- | --- |
| `GPT 5.6`, `SONNET 5`, etc. under the name | Current model. `◷ 200K` or `◷ 1M`, when present, is the model's total working-memory capacity. |
| Thin line below the model | **Context remaining**. Longer means more room; below 20% it becomes redder. It is absent when telemetry cannot be obtained. |
| Black strip | Current terminal display, useful for judging whether the agent is working or waiting. |
| `ORD` | Most recent instruction assigned to the agent. |
| `RX` | Most recent instruction received through agent-mail, with sender, subject, and importance. The row is absent before any mail arrives. |
| `● ONLINE` | The agent process is running. **This does not necessarily mean it is currently making progress.** |
| Top-right state | Yellow seconds indicate work; faint `LAST …` indicates waiting for input; `?` and `APPROVAL` mean human intervention is required. |
| `↩ EXIT` | Send graceful `/exit` to a running agent. Press twice to confirm. |

The next image has grown to 12 cards, and cards such as Warm-Lovelace show `RX`. Comparing `ORD` and `RX` separates “assignment” from “the last thing received and its sender.”

![DECK with 12 agents and ORD and RX on several cards](images/deck_growing.png)

### Separating header counters from card states

The three header items are **counts**, not descriptions of individual work.

| Counter | What it counts |
| --- | --- |
| `RUNNING` | Cards whose agent process is running |
| `STANDBY` | Active-agent cards whose process is not running |
| `AGENTS` | Total active agents |

Therefore, `RUNNING 12` alone does not mean all 12 agents are progressing. **Read overall counts in the header and each agent's working / waiting / intervention state from the card's top-right and border.**

### Do not miss human intervention

In the next image, look at the `?` on Bright-Curie's card and the red border plus `APPROVAL` on Swift-Noether's card.

![DECK waiting for intervention, with a question mark on Bright-Curie and red APPROVAL on Swift-Noether](images/deck_humanloop.png)

| Signal | Meaning | How to clear it |
| --- | --- | --- |
| `?` | The agent stopped to ask the user a question. | Enter a choice or answer in the terminal. |
| Red `APPROVAL` border | The agent is waiting for permission approval. | Approve or reject in the terminal. |

Neither progresses until cleared. The process remains alive, so the image still says `RUNNING 12`. This is why header counts and card states must be read separately.

### Opening the corresponding terminal

Click a card to open its detail panel. For a tmux-based Claude / Codex CLI agent, click `OPEN TMUX` at the panel's upper right to raise an existing terminal or open a new one and attach. After clicking a node in NETWORK, the same procedure applies.

### Categories

- `running`: an agent process is active in the tmux pane
- `standby`: the session exists but is waiting
- `finished`: the agent process ended but its session / shell remains
- `gone`: a mail record exists but no tmux session exists
- `retired`: soft-retired

Running is not inferred from mail `last_active` alone. tmux process, pane state, and session state are combined so past sessions are not misreported as currently running.

### Search

Top-bar `FILTER · name / task` searches not only names but also **task descriptions, live pane titles, and the subject and sender of the last received instruction**. If you remember what an agent did, you can find it without remembering its name.

By default only running and finished agents appear. Enabling `show all` adds `gone` / `retired` agents from the last 30 days, supporting the pattern of **finding a finished agent through search and resuming it**. This preserves a counterpart with old context for later restart; see “Resume a finished agent” under [Find an action](#find-an-action) for procedure and [After a child completes](#after-a-child-completes) for appearance.

### Card actions

- History panel: Claude / Codex transcript
- Output panel: `LOG_*.md` and artifacts under the project or configured roots
- terminal open / focus
- two-step-confirmation EXIT for a running agent; `/api/exit` itself also accepts finished agents
- KILL / soft retirement for finished / gone agents with no attached tmux client

KILL eligibility is not based only on frontend appearance; the server rechecks the `build_agents()` category. The UI hides KILL for a session with an attached client, and even a direct API call gets a hard `refusing to kill (detach first)` refusal.

### After a child completes

In a normal completion flow, a child started by `/delegate` sends an agent-mail completion report to its parent before exiting. The parent reads the report, verifies the artifact, and then returns the result to the user. After the child REPL ends, launcher cleanup releases reservations, soft-retires the remote identity, and removes child runtime credentials and state. The tmux session closes when that command ends.

Thus a completed child's card disappears from the normal DECK view, but this is not a failure. Enabling `show all` displays `gone` / `retired` agents from the last 30 days.

![DECK show all — FINISHED / GONE / RETIRED sections](img/deck-show-all.jpg)

NETWORK overlays current runtime state with the selected time window. Completion or retirement alone does not hide a node immediately, but when last activity leaves the window, the child node and connected spawn / mail edges disappear. Absence from the current window alone does not mean task failure. Use NETWORK `ALL` for history and DECK `show all` for an individual final state.

## Output / deliverables

Output is not vault-specific. It recursively scans `:`-separated roots in `AGENTSTACK_DELIVERABLE_ROOTS`, or the project's `logs/` when unset, and displays up to 25 `LOG_*.md` files whose frontmatter `agent:` matches the selected agent, newest mtime first.

The project base falls back in the order absolute `AGENTSTACK_PROJECT_KEY`, absolute `AGENTSTACK_VAULT`, then dashboard cwd / Git root. Explicit deliverable roots replace the defaults.

A detected item inside `AGENTSTACK_VAULT` becomes an `obsidian://` link; a generic project / shared log outside it appears as a non-link item. The list and artifact count work without Obsidian.

## Codex App runtime

With [Codex App integration](codex-app.en.md), the dashboard reads the Bridge's allowlisted snapshot alongside tmux state. It promotes a row with the same agent-mail name to `surface: codex-app` and displays `Codex App · <state>` or `Codex App · wake:<status>` live.

- `registering / working / waiting / blocked`: treated as running
- `dormant / degraded`: treated as finished
- an active state with no snapshot update for more than ten minutes: treated as dormant to avoid stale running state
- capability: `open` only

A Codex App runtime has no tmux pane. The dashboard performs no terminal attach, EXIT / KILL, or wake; jump / resume raises the macOS ChatGPT app. The Bridge, not the dashboard, owns inbox cold wake and delivery retry.

## NETWORK

NETWORK overlays “who spawned whom” and “who communicated” in one force graph. Read **nodes as agents and lines as relationships**.

The initial state below has three nodes and `0 links · 0 spawn`, so there are no parent-child relationships or communications yet. The right-side `TUNE` panel adjusts layout.

![Initial NETWORK state with three nodes and no communication or spawn lines](images/net_start.png)

### Nodes

- One node is one agent. The small badge beside the portrait identifies the provider.
- The **lineage-colored ring** around the portrait shows spawn position. The top-left `?` legend maps parent / child / both; `both` is an intermediate node receiving work from a parent and delegating onward.
- The outer arc is **context remaining**. It spans up to 270 degrees and shortens as less remains.
- Hover, or long-press on touch, shows task, live state, model, and last activity.
- Click a node for its detail panel. From a tmux agent, click `OPEN TMUX` to go to its terminal.

History in the detail panel contains transcript and a 24-hour event sparkline; Output contains project-scoped artifacts. ROLE ASSIGN saves or removes role / group annotations.

![Node detail panel — CTX / STATE / History / ROLE ASSIGN / transcript](img/agent-detail.jpg)

### Edges and mail

- spawn edge: parent-child lineage
- communication edge: agent-mail message
- number on a communication edge: message count between the pair
- arrow on a communication edge: direction
- click an edge for the pairwise mail drawer
- drawer contents: subject, importance, time, body
- live messages use comet animation

The next image shows 12 nodes with `6 links · 6 spawn`. Lineage-colored rings distinguish parents, children, and intermediates, while `1` on an edge means one communication for that pair.

![NETWORK with 12 nodes, parent-child spawn lines, and message-count-one lines](images/net_growing.png)

As communication increases, the number on the same pair's edge rises. The next image shows `29 links · 9 spawn` and `2` or `3` near the center. These numbers are **message counts**, not node or child counts.

![NETWORK with more communication and message counts two and three on edges](images/net_humanloop.png)

Without `AGENTSTACK_PROJECT_KEY` / `AGENTSTACK_VAULT`, mail edges and the drawer show `NOT CONFIGURED`. tmux telemetry remains, distinguishing missing mail configuration from a complete dashboard failure.

![Mail drawer after an edge click — pairwise subject / importance / time / body](img/network-edge-drawer.jpg)

### Display controls

- time-window slider / ALL
- legend
- node search
- TUNE: `NODE SIZE`, `LINK DIST`, `LINK WIDTH`, `REPEL`, `CENTER`, `LINK FORCE`; right-side sliders adjust node layout
- save TUNE values in `localStorage`
- dense mode above 300 nodes

Dense mode hides labels, annotations, provider badges, and context arcs to reduce rendering load for large graphs.

## SELECT and bulk actions

SELECT mode supports selecting several nodes by drag rectangle or node click.

| Action | Target | Behavior |
| --- | --- | --- |
| EXIT | running / finished | graceful `/exit` through `/api/exit` |
| RESUME | gone / retired | `/api/jump`; transcript resume when tmux is absent |
| REPLAY | two or more agents with mail history | DIGEST REPLAY |

EXIT / RESUME require two-step confirmation and are sent sequentially at 60 ms intervals. They are not parallel requests without a safety valve, avoiding accidental operations and service spikes.

![SELECT mode — 13 agents selected by rectangle, with EXIT / RESUME / REPLAY below](img/network-select.jpg)

## DIGEST REPLAY

![DIGEST REPLAY — chronological events for 11 selected agents](img/digest-replay.jpg)

Chronologically replays mail, spawn, exit / retire, and approval-wait events for selected agents.

- play / pause
- seek and event markers
- absolute / relative clock
- logarithmic speed from `×1` to `×10000`
- message-card HOLD from `0.1s` to `15s`
- GROUP-ONLY
- TIME-TRAVEL

TIME-TRAVEL ON reconstructs nodes, edges, and state from the initial snapshot. OFF replays only comets on the current graph.

The history range auto-fits to the oldest and newest events, widening ranges too short to operate. Large histories are sampled with priority for topology and in-group mail.

`Esc` / CLOSE restores the live graph snapshot and mail polling.

## NEW AGENT

![NEW AGENT modal — launch manifest for identity / engine / directory / task](img/new-agent.jpg)

`+ NEW AGENT` is a launch manifest that confirms identity, engine, directory, and task in order. Common fields form a one-way path; parent / role / group / isolation are folded into ADVANCED, creating the shortest path for a standalone agent.

### Identity

- `AUTO`: the server verifies an available `Adjective-Scientist` from shared vocabulary and sends that explicit `name` in the registration request
- scientist rail: portrait and `available / occupied / unknown`
- scientist selection: `/api/suggest-name` attaches an available adjective and verifies it against the live registry
- SHUFFLE: suggest another verified name with the same scientist
- `occupied / unknown`: cannot select
- scientist outside roster or no available candidate: HTTP 409 prompting another scientist / AUTO

Scientist-rail `available` means that at least one pairing with the 134 adjectives is available, not that the bare surname is free. Adjectives are synchronized with agent-mail canonical `SIMPLE_ADJECTIVES`, and the client does not create unverified local names. AUTO also fail-closed verifies up to 75 candidates against the live registry and rejects spawn when none can be confirmed available.

### Engine

- Claude / Codex provider tabs
- model cards and usage guidance per provider
- Claude: Sonnet / Opus / Haiku
- Codex: `gpt-5.6-sol / terra / luna / gpt-6-astra`
- Codex effort: `low / medium / high / xhigh`, default `xhigh`

The server uses the provider / model / effort allowlist for both catalog and validation.

### Directory

Displays `AGENTSTACK_SPAWN_DIRS` preset chips and an exact-path input. Persist presets with installer `--spawn-dirs`; shell `export` does not reach the service. See “Spawn directory” in [configuration](configuration.en.md). The input uses root-scoped `/api/fs/dirs` typeahead and supports arrow keys / Enter. The last directory is saved in `localStorage`.

Typeahead does not leave `AGENTSTACK_SPAWN_ROOTS` or suggest hidden directories, `..`, or symlinks outside a root.

### Task and ADVANCED

Task is required and limited to 4,000 characters. ADVANCED open/closed state is saved in `localStorage`.

- parent: default `STANDALONE · independent agent`
- role: optional, at most 40 characters
- group: optional, at most 24 characters
- isolation: isolated worktree and base revision

With no parent, the request sends `standalone: true` and starts an independent agent without `PARENT_AGENT`. Selecting a parent creates a normal child, sends the task to the child's inbox, and leaves a CC audit trail for the parent.

### Spawn sequence

1. Create child identity and dedicated token with `register_agent`
2. Apply role / group annotation best-effort
3. For a normal child only, create a task message with parent as sender and a CC audit trail
4. Save the token in a mode-`0600` one-shot file
5. Start `spawn_child.sh --pre-registered` in the background
6. Wait up to 120 seconds for launcher readiness verdict
7. Recheck the live tmux session

On failure, clean up the tmux session and token / child credential file and include the end of `dashboard/logs/spawn.log` in API error `detail`. A registered identity is retained because the server lacks delete authority, explicitly reported as `registration_retained: true`.

For Codex, pass `--codex --model <model> --effort <effort>`. The non-Git-directory trust dialog is accepted with `C-m` up to ten times and fails fast if it remains.

Every POST requires a JSON body. Browsers must be same-origin; CLI requests are accepted only without `Origin` / `Sec-Fetch-Site`.

### Isolated worktree

With `worktree: true`, each child uses:

```text
/tmp/cc-worktrees/<child-name>
branch: exp/<child-name>
```

Omitted `worktree_base` means `HEAD`. The task message names the original project key, branch, base, and directory so the worktree path is not mistaken for the agent-mail project key.

## Embed mode

`/?embed=1` or a same-origin iframe uses embed mode with a compact header.

From the parent window:

```js
frame.contentWindow.postMessage({type: "net-pause"}, location.origin);
frame.contentWindow.postMessage({type: "net-resume"}, location.origin);
```

- `net-pause`: stop DECK / NETWORK / mail-health polling
- `net-resume`: restart polling and immediately refresh the current view

This contract prevents a hidden iframe from continuing to poll tmux / SQLite. Only same-origin messages are accepted.

### Theme axis bridge

A same-origin parent can send a one-axis A/B override to the dashboard alone. The parent owns selection history and presets; the iframe does not persist them.

```js
frame.contentWindow.postMessage({
  type: "agentstack-theme-axis",
  version: 1,
  axis: "glow",
  value: 1,
}, location.origin);
```

`axis` is one of `dim-contrast`, `small-text`, `tracking`, `glow`, and `background`. A numeric value must be finite with `0 <= value <= 1`; out-of-range values are rejected as `invalid-value`, not clamped. `null` physically removes the override style node and temporary attributes. Initial display applies no Bridge CSS and remains on the legacy rendering path.

The receiver checks both `event.source === window.parent` and `event.origin === location.origin`. A processed request returns this envelope to the parent.

```js
{
  type: "agentstack-theme-axis-result",
  version: 1,
  ok: true,
  requested: {axis: "glow", value: 1},
  state: {axis: "glow", value: 1},
  source: {unit: "declaration", expected: sourceRecords.length, matched: sourceMatched},
  mutation: {unit: "effect-component", expected: snapshot.length, applied: appliedCount},
  effect: {unit: "effect-component", evaluated: 30, changed: 26,
    rendered: 28, inViewport: 26, visibleExpected: 26,
    visibleReached: 26, visibleChanged: 26, deferred: 4},
  reason: null,
}
```

The identifiers above are pseudocode illustrating relationships within the envelope. The source of truth is [`dashboard/theme_effect_manifest.json`](../dashboard/theme_effect_manifest.json), generated from `dashboard/index.html` by [`scripts/dashboard_theme_manifest.py`](../scripts/dashboard_theme_manifest.py), plus the runtime inventory generated from it and embedded in HTML. The former is a review record containing selector / property / line / component; the latter has stable ID lists for those records and rule/source digests. Runtime `source.expected` is derived from each axis's `records.length` and has no independent numeric constant. After changing CSS, run the following and review changes to the record list, rule digest, and source digest together.

```bash
python3 scripts/dashboard_theme_manifest.py --write
python3 scripts/dashboard_theme_manifest.py --check
```

`source` is token / declaration coverage; `mutation` is the compiled count of token-write / element / effect-component applications derived from an immutable snapshot immediately before application. Source units are axis-specific: `dim-contrast` / `background` use `token-write`, while `small-text` / `tracking` / `glow` use `declaration`. Effect membership is fixed only by generated eligibility's pre-apply live match; neither pre/post/endpoint values add or remove targets. Of these, rendered members with nonzero boxes inside the viewport are `visibleExpected`; members whose post-cascade computed value reaches the requested derivation are `visibleReached`. A member already at the requested value also counts as reached. `visibleChanged` counts visible members whose canonical post differs from pre; `changed` is an independent no-op gate counting that difference for all evaluated members. Hidden / zero-box / offscreen members count as `deferred`. A non-null apply succeeds only with `visibleExpected > 0`, `visibleReached === visibleExpected`, `visibleChanged > 0`, and `changed > 0`. Zero or sub-quantization changes are rejected as `no-effective-change`, zero visible targets as `no-visible-targets`, and any member failing to reach the requested derivation as `effect-count-mismatch`, restoring the previous valid axis. Surfaces and units are not summed. The `glow` effect unit is a live `effect-component`: two target radial layers on one surface count as two, and one CSS rule matching ten visible elements counts as ten. Selector and keyframe source components are divided into `emissive | elevation | focus | state`, allowing color halos to weaken while preserving elevation and focus.

### Atomic theme profile bridge

`small-text` and `tracking` can be applied together as one atomic profile using a complete five-axis vector. Every key is required; non-null combinations may be empty, any subset of `small-text` / `tracking`, or a single legacy axis.

```js
frame.contentWindow.postMessage({
  type: "agentstack-theme-profile",
  version: 1,
  requestId: "theme-42",
  values: {
    "dim-contrast": null,
    "small-text": 0.5,
    tracking: 0.25,
    glow: null,
    background: null,
  },
}, location.origin);
```

The receiver derives both axes from the same A snapshot with every experimental override removed. With baseline font size `s0` and baseline letter-spacing ratio `r0`, final small size is `s(vs)`, tracking ratio is `lerp(r0, min(r0, 0.08), vt)`, and final spacing is that ratio times `s(vs)`. Because the previous profile render is not used as the next baseline, UI operations small→tracking and tracking→small produce identical computed size / weight / spacing for the same complete final vector. DOM nodes added later also derive both axes together from an A baseline with overrides temporarily disabled and add them to each axis's independent envelope.

On success, it returns the response below. `requested` / `applied` are always exact five-key maps; `axes` is the exact set of non-null axes.

```js
{
  type: "agentstack-theme-profile-result",
  version: 1,
  requestId: "theme-42",
  surface: "telemetry",
  requested: {"dim-contrast": null, "small-text": 0.5,
    tracking: 0.25, glow: null, background: null},
  applied: {"dim-contrast": null, "small-text": 0.5,
    tracking: 0.25, glow: null, background: null},
  status: "applied",
  axes: {
    "small-text": {status: "applied", source: {}, mutation: {}, effect: {}},
    tracking: {status: "applied", source: {}, mutation: {}, effect: {}},
  },
}
```

If any axis fails a source / mutation / effect guard, the complete profile is rejected and `applied` contains the restored last-valid complete map. Schema violations also leave state unchanged. Legacy `agentstack-theme-axis` v1 is equivalent to a complete profile with every axis except the target set to null, and legacy `value: null` resets all axes. If the result reply is lost, the parent coordinator can send the last-valid complete profile with a fresh `requestId` as a compensating request.

After embed initialization and reload, the dashboard sends `agentstack-theme-axis-ready` and `agentstack-theme-profile-ready`, both version 1 with `surface: "telemetry"`, to the same-origin parent. Recovery sends a compensating profile after this explicit signal and does not count as complete until its result is acknowledged. The parent coordinator owns timeout and UI failure display when ready never arrives.

## Terminal bridge

Opening a terminal from a card or node makes the server allocate a Ghostty / iTerm2 / Terminal.app jump or `ttyd` browser terminal.

`AGENTSTACK_BIND_HOST=0.0.0.0` also exposes the terminal bridge and control endpoints externally. Because the dashboard has no authentication layer, do not use it outside a trusted LAN / VPN.

## Demo (no server required)

The public [agentstack-demo.pages.dev](https://agentstack-demo.pages.dev/) runs this exact `index.html` without Python, SQLite, or tmux; it is not a copy. The screen's communications are GET requests through `fetch` only, so `dashboard/demo/demo_api.js` intercepts `fetch` and constructs time-dependent responses from scripts (`story_*.js`, roles, life/death, mail bodies, and work logs). Write APIs are disabled.

- Play the same demo locally at `http://127.0.0.1:8770/?demo=1`; `demo_tour.js` only overlays captions and target rings and does not change product behavior
- The script format is [`dashboard/demo/STORY_CONTRACT.md`](../dashboard/demo/STORY_CONTRACT.md). Publicly cleared portraits are listed in `PORTRAITS_CLEARED.txt`, and the build does not bundle unlisted portraits
- Build a static bundle with `bash dashboard/demo/build.sh [outdir]`; it works at any path. Every script gets a content-hash version, preventing old CDN copies from pairing with new HTML

## Related documentation

- [Hooks and operational helpers](hooks.en.md)
- [Codex App integration](codex-app.en.md)
- [API reference](api.en.md)
- [Configuration](configuration.en.md)
- [Troubleshooting](troubleshooting.en.md)
