# Design language

> 日本語版: [design.md](design.md)

[Previous: Dashboard](dashboard.en.md) · [Back to README](../README.en.md)

This document puts into words the decisions behind how the ORRERY Telemetry dashboard looks and moves. It is the canonical record of the design language the maintainer has built up through the existing implementation, not a proposal for deciding one.

Read it before adding to, or changing, the dashboard UI, and before asking an AI to build UI for it. Compose with the vocabulary here wherever it fits; where it does not, open an Issue before writing code. When you hand the work to an AI, the shortest path is to have it read this file first.

The parent document is [DESIGN.md](https://github.com/gyroid-eth/orrery/blob/master/docs/DESIGN.md) in the cockpit (`orrery`) repository. Telemetry is the instrument panel embedded in the cockpit; opened on its own it must still read as the same language.

One note on how this is written. Each section carries both the principle and the current state of the code. The principle is what new UI must follow; the current state lists the exceptions that already exist. An existing exception does not loosen the principle, and exceptions are not added quietly.

## 1. Concept: an instrument panel

ORRERY is a tool for watching and operating a crew of agents that carry scientists' names, on a dark instrument panel. Telemetry is the panel's observation surface: who is working now, who spawned whom, who sent what to whom, all readable on one screen.

Surviving long stretches of monitoring comes first. The screen is quiet; only meaningful places glow, and only places with activity or attention move. No cyberpunk neon, no decoration across the whole screen. The identity is precision (telemetry) living together with humanity (portraits), never one at the expense of the other.

## 2. `index.html` has two layers

The style in `dashboard/index.html` comes in two layers. The upper one is old; the lower one is the current language.

| Layer | Where | What | Status |
| --- | --- | --- | --- |
| CRT phosphor layer | First half of `<style>` (`--void` `--bone` `--cyan` `Chakra Petch`, clip-path notches, scanlines, glows) | The original "phosphor tube instrument"; the structural and dimensional definitions also live here, and so does the NEW AGENT modal's current material and controls (the `Cockpit spawn v2 parity` section), appended there historically | Left in place. New UI does not reference its decoration. Structure (grid, bay assembly, modal skeleton) is defined here, so read it too when you need dimensions |
| ORRERY surface layer | From `/* ORRERY telemetry surface */` onward: the `:root` and the overrides | The cockpit's tokens, typefaces, and surface material | The current language. New UI is written only in this vocabulary |

The two layers are a sketch of the history, not a rule for telling old from new by position. Effective values come from the whole cascade, so read both when confirming a dimension or a state.

The ORRERY surface layer aliases the old names (`--void` `--bone` `--cyan` `--line` …) to the new tokens (`--cyan: var(--amber)` and so on) and overrides the old CSS. An old name therefore still resolves to a new token's value, but it is misleading and blocks the eventual removal of the old layer, so new CSS does not use it. Aliasing does not replace literal colours: colours written literally in the old layer (the cyan chip borders, the ASK red, the portrait scanlines) still reach the screen and are not swapped by a theme. Each is either an intended exception or a leftover not yet cleaned up; none is a model for new CSS. A literal colour in new CSS (`#ffb02e` `#36e8ff` `rgba(242,182,90,…)`) escapes the theme, and `Chakra Petch` leaves the type system of §3.

## 3. Typography

| Role | Face | Examples |
| --- | --- | --- |
| Proper names and headings | `--serif` (Fraunces) | wordmark, agent names, the header statistics, modal titles |
| Everything else | `--mono` (IBM Plex Mono) | labels, chips, paths, times, body text, edge counts in NETWORK |

Serif is for things that have a name. An agent's name is a person's name, so serif; a statistic is an instrument needle, so large serif; a label is a stamp on a machine, so small mono capitals. No third face is added. Confirm nothing collapses when the font CDN is unreachable (Georgia / ui-monospace fallbacks).

Current state: a few old-layer rules (the initials shown when a portrait is missing, drawer headings) still name `Chakra Petch`, but that font is not loaded, so they fall back to sans-serif. New UI does not write it.

Sizes (dark implementation values):

- wordmark 20px / 600 / .12em tracking; the subtitle `TMUX · ORRERY MAIL` 9px / .2em
- labels, chips: 9–9.5px, .12–.22em tracking, uppercase
- body: the current line 12px, ORD / RX 11.5px (line-height 1.5), inputs 13px
- agent name 17px / 500, header statistics 18px / 500, NEW AGENT title 24px / 500, detail name 19px
- Principle: create nothing below 9px. Current state: modal section labels 8.5px, identity status 6.5px, network roles 6.8px, EXIT / KILL 8px already exist

## 4. Colour

The whole surface is near-monochrome. Hue carries meaning, so its uses are limited.

| Token | Dark value | Meaning |
| --- | --- | --- |
| `--bg` | `#0b0d11` | ground |
| `--panel` / `--panel-2` / `--elev` | `#111419` / `#161a21` / `#1c212a` | surfaces; lighter the higher they sit |
| `--ink` / `--ink-dim` / `--ink-faint` | `#ece5d6` / `#9a9384` / `#5f5c52` | text: primary / secondary / stamped |
| `--hair` / `--hair-2` | ink at 8.5% / 4.5% | hairlines; the only surface boundaries |
| `--amber` / `--amber-glow` | `#f2b65a` / 22% | attention and selection; the one warm accent |
| `--alert` | `#d87863` | needs a human (ASK, context nearly exhausted) |
| `--ln-local` / `--ln-remote` / `--ln-delegate` | `#5fb3a3` / `#b58be0` / `#83b06a` | spawn lineage; parent / child / both in NETWORK |

Rules:

- Hue is reserved for spawn lineage (who started whom). New features do not get new colours. The one current exception is a faint 4.5% local-colour grade on the body background
- Amber goes only where you want eyes right now: the selected tab, focus, a WORKING card, an attention band. More than a tenth of the screen in amber is too much
- Working / waiting / idle are never told apart by colour alone; motion, shape, and text are used together
- The exception is the context-remaining gauge. The DECK hairline shifts from ink to amber as context runs down; the NETWORK arc is amber and turns alert below 20%. It never shares a ring with lineage colour
- UI state never depends on portrait colour. Portraits are grayscale; state lives outside them (ring, LED, chip)
- Current state: only the NETWORK intervention glyphs draw `!` in literal red and `?` in literal cyan. That is an intended exception so the two meanings stay distinguishable, and it is not extended

## 5. Surfaces and hierarchy

The information hierarchy is fixed, top to bottom. A new element declares which level it belongs to before it is built.

1. header (wordmark, DECK / NETWORK toggle, RUNNING / STANDBY / AGENTS statistics, MAIL health, search, history range, NEW AGENT)
2. sector heading (a mono capitals line such as `ACTIVE AGENTS [ 27 ]` with a rule fading to the right)
3. bay (the agent card)
4. auxiliary bands (history sparkline, usage, …; weaker than agent cards, collapsible)
5. overlays (NEW AGENT modal, agent detail, edge thread drawer, Tune)

### Header and grid

- The header is sticky, minimum 58px, padding 9px 22px, 22px between items. Statistics stack "number → label → 2px underline" with 16px between them. The DECK / NETWORK toggle and MAIL are pills (999px radius, hairline; the ground is `--panel` at 74% for the toggle and 62% for MAIL). Search is 226px wide. NEW AGENT is amber at 22% with a 32% amber border, 7px radius, amber text
- main has padding 22px 22px 0, `repeat(auto-fill, minmax(440px, 1fr))`, 10px gaps. At 760px and below it is one column with 13px sides, and the statistics are hidden
- The sector heading has margin 16px 2px 4px, 9px / .22em, `--ink-dim`, a 7px amber diamond on the left, and a `--hair` rule fading to the right
- Embedded in the cockpit (`body.embed`) the header drops to 44px and hides the wordmark, statistics, and MAIL. Information the cockpit already shows is not shown twice

### Anatomy of a bay (agent card)

```text
┌ portrait 56px ┐  ✳ AgentName            LAST 1M41S
│ grayscale     │  ↯ asked for Agent-Name
└───────────────┘  [ GPT 6 ] [ ● LINKED ] [ ● ATTACHED ]
──────────────────────────────────────── ctxline (thin context-remaining line)
│ current line (amber rule on the left)
ORD  summary of the parent's request
RX   one line of the most recent mail received
──────────────────────────────────────── foot
● ONLINE  LAST 1D AGO  ZSH                    [ ↵ EXIT ]
```

- Surface: `--panel` with a 2.5% white grade at the top edge only, `--hair` border, 9px radius, shadow `0 10px 32px rgba(0,0,0,.12)`, padding 14px 17px 13px, 9px between rows. The old layer's notched corners (clip-path) and amber corner marks do not appear
- The top row is horizontal with a 13px gap. The portrait is 56px with a 9px notch, grayscale at contrast 1.06, or the first two letters of the name when missing. The identity column on the right runs name → requested-name note (only when the registered name differs) → chips, 8px apart. The provider logo sits 13px left of the name, at opacity .55 when the agent is not running
- Chips are 9.5px, 1.5px tracking, padding 2px 7px, wrapping with 6px gaps. The principle is neutral colour, with category in amber and importance in alert. Current state: `LINKED`, `ATTACHED`, and the context-window chip use amber text with a cyan-derived border and glow, and the delivery chip is amber; these are leftovers of the old layer, and new chips do not copy them
- The "current line" carries a 2px amber rule on its left, padding 7px 11px, one line with ellipsis. It is the agent's "now". With no live information it reads `— STANDBY · no live activity —` in italic on a hairline. The amber rule is this line's mark and is not used elsewhere (current state: the sparkline tooltip and the detail's tool bubble also carry it)
- Key-value rows (ORD / RX): keys in amber 9.5px uppercase, 34px wide, 10px from the value. Values are `#c8c2b1` in dark and `--ink-dim` in light. A row appears only when it has data
- ctxline appears only while running with a known remaining value: 2px high, width equal to the remaining percentage. 50% and above runs ink-dim → ink, 20–50% ink-dim → amber, below 20% amber
- The foot has a `--hair-2` rule above, padding-top 9px, 10px / 1.5px tracking. Left: `● ONLINE` (`○ SHELL` when stopped), elapsed time, shell name. Right: a 17px-high EXIT (while running) or KILL (finished / gone and not attached). Both are two-step (arm → confirm, released after 5 s)
- Sinking by state: idle / infra / warmup sink the whole card to opacity .5, hover restores .85. finished / gone / retired leave the card and sink the provider logo to .4 / .4 / .28. Nothing is removed
- Hover lifts the card 2px onto `--panel-2` with a 28% amber inner border. Click opens the detail. DECK has no "selected" state (the selection ring is NETWORK only)

### Auxiliary bands

Bands that sit above or between agent cards (history, usage, notices) rank below the cards. This part is principle only: the usage band is not implemented yet (proposed in #31). Do not treat its dimensions as implementation values.

- Keep their height within what does not push the first row of agent cards down, and make them collapsible. Collapsed, the key numbers stay beside the header
- Never show a stale value as current. Always show the observation time; for a provider that could not be read, say so in words, as in `WAITING FOR UPDATE`
- Show only the windows the provider actually returned. Do not assume a 5h / 7d frame on the ORRERY side and fill blanks

## 6. State and motion

| State / event | DECK (bay) | NETWORK (node) |
| --- | --- | --- |
| running | provider logo lit amber, breathing over 2.8s (opacity .7 ↔ .57). While WORKING, a 135° amber tint on the card (5.5% → 1.8%); only when a turn duration or the context value is known, a 9px square LED top-right blinking at 1s with the duration, or `WORKING` when no time is known | portrait, edge, and dot at 1.2×. While WORKING a motion ring (dash 30 / 70) orbits outside the context arc every 1.5s |
| waiting (for input) | only when the previous turn's duration is known, `LAST` and the time top-right, still, `--ink-dim` at opacity .55; nothing otherwise | no scan; the arc shows only the remaining value; still 1.2× |
| ask (needs a human) | top-right `APPROVAL` in red at 1s, LED at .6s; red frame, shadow pulsing at 1.6s | a solid red ring with an italic `!`; the glyph bounces at 1.25s |
| question | top-right `?` in amber blinking at .9s | a solid cyan ring with an italic `?`; the glyph bounces at 1.6s |
| finished / gone / retired | logo at .4 / .4 / .28 (§5) | whole node .4 / .42 / .28; portrait scaled to .78 / .72 with shading .54 / .54 / .66 |
| mail arrived | RX line updates | a comet runs along the edge and an arc spreads from the receiving node |
| selected | none (click opens detail) | amber ring |

- Motion shows activity and attention. It keeps going while an agent is running or asking, but nothing moves as decoration
- Periods are fixed per element; the sub-second ones are the blinking LEDs (.6–1s), question (.9s), the confirm arm (.8s), and the arrow inside a submitting SPAWN button (.7s). The caret on the current line blinks at 1.05s. No new element gets a sub-second period
- `prefers-reduced-motion` is honoured in 6 CSS places and 2 JS places. Current state: two kinds of motion do not stop. The DECK logo breathing and the question indicator have no reduce rule at all; the state LEDs and `APPROVAL` are outranked by a more specific rule. Every new motion must stop under reduce, and state must still read from text and shape

## 7. NETWORK view

- A node is a portrait medallion. Base radius 13 (SVG units, scaled by Tune's NSIZE and zoom). The edge is lineage colour at 1.55; the context arc sits at radius +5, stroke 1.55, at most 270°; the motion ring at radius +9, stroke 1.6. No arc when context is unknown, a dot when the portrait is missing
- Labels are serif (name 10.5px, running 11px) and mono (role 6.8px, counts 9px). A node whose registered name differs from the requested one shows mono 10px with a dotted amber underline. Above 300 nodes (dense) names, roles, counts, badges, and arcs are hidden. When many nodes make labels collide, disclose on hover / focus by highlighting the neighbourhood instead of showing more labels all the time
- Edges are thin `--ink-dim` lines at opacity .22. Spawn edges are solid `--ln-delegate` at 1.25 / .4. Mail counts sit at the edge midpoint in mono with the ground colour burned in behind the glyphs
- The background is a 48px grid and a faint central ellipse. The grid is `--hair-2` and never louder than the ground
- Floating panels (Tune, legend, sel-toggle) share one glass material: `--panel` at 90%, hairline, 9px radius, shadow 0 16px 42px black 18%. Only the legend popover, being large, uses a slightly heavier material: 10px radius, 9% white border, blur 26px, shadow 0 30px 90px black 52%

## 8. Overlays (NEW AGENT modal and others)

NEW AGENT, agent detail, and the edge thread drawer share one glass material. What they share is the material; placement and dimensions differ per overlay. The placement, dimensions, and controls below are NEW AGENT's.

- The material is neutral: a 1px 9% white border, 13px radius, a ground that grades from 4.5% white to transparent over 140px on top of `rgba(17,19,25,.66)`, blur 26px with saturate 1.12, shadow `0 30px 90px rgba(0,0,0,.52)`. The old layer's amber frame is overridden away and its corner marks are `display:none`; neither appears
- The backdrop is `rgba(8,9,13,.62)` with blur 10px (NEW AGENT and detail only). The NEW AGENT frame is centred, `min(790px, 100vw - 48px)` wide, at most 92vh high, scrolling inside. Detail is `min(1180px, 100vw - 48px)` wide and `min(88vh, 900px)` high. The drawer is fixed to the right edge (top 68px, bottom 14px, width `min(390px, 100vw - 18px)`), rounded on the left only, with no right border
- Header padding 17px 19px. Kicker `NEW AGENT` 8px, title `LAUNCH AN AGENT` serif 24px / 500, subtitle `IDENTITY · ENGINE · DIRECTORY · TASK` mono 8.5px. The top-right × is 30px square
- The body is a two-column grid (gaps 12px 16px, padding 18px 19px) with the four main sections full width. identity / engine / directory have a `--hair` border, 10px radius, a 30% near-black ground, padding 12px; task has no frame. Section labels are 8.5px / .14em with a short 11×1px amber line on the left (not `▸`)
- identity is optional: an AUTO NAME preview (44px round portrait) and a horizontally scrolling scientist strip (68px wide, 42px round portrait, 8px radius). Selection is amber at 22% with a 55% border; unavailable entries are at opacity .3
- engine is provider tabs (minimum 108px) and model cards (minimum 155px, 7px gaps), plus effort chips for providers that need them. The options come from the catalog and are not enumerated here
- directory is preset pills beside a typeahead input. task is a required textarea (minimum 96px high, line-height 1.55). Inputs are 13px, padding 8px 11px, 7px radius, ground `rgba(11,13,17,.52)`; focus adds an amber border and a faint glow
- Below the four sections, a collapsible ADVANCED. The foot has a rule above and padding 13px 19px. CANCEL is hairline only; SPAWN is transparent with a 40% amber border and fills amber on hover / focus. The primary action is not filled at rest so that the fill can mean "pressable"
- Closes on backdrop click, `Esc`, and × (not while submitting). Current state: focus is not returned after closing. Destructive actions (KILL / EXIT) on the card are two-step (arm → confirm, released after 5 s). Current state: detail also has an EXIT, and that one submits in a single step. The principle is two steps; the detail side has not caught up

## 9. Light theme

Telemetry has two themes, dark and light. Light is a warm-paper cream ground on which text, surfaces, hairlines, and the accent all take values different from dark. Light is what the maintainer uses day to day.

The light palette and the code that applies it ship with telemetry. `dashboard/theme_core.js` derives the palette from OKLCH seeds, `dashboard/theme_controller.js` writes `html[data-color-theme="light"]` and the token values, and `dashboard/theme_light.css` adds the light-only corrections. The default is dark, and there are two ways to switch today:

- Embedded in the cockpit (`orrery`): the host announces the theme by same-origin `postMessage` and the controller applies it
- Opened on its own: there is no user-facing toggle yet. From the developer console:

```js
window.AgentStackColorTheme.apply({preference: 'light', resolved: 'light'})
window.AgentStackColorTheme.apply({preference: 'dark', resolved: 'dark'})
```

Planned: standalone telemetry will switch between dark and light from the header (with the choice persisted, and the cockpit's setting winning when embedded). Alongside that, the literal colours left in the old layer will be replaced with tokens; today the cockpit gets by rewriting those literals at runtime when it embeds telemetry.

Only one mechanism makes themes work: colours are written by name (the tokens of §4), and a theme swaps the values behind the names. So the rule in §4 and §10, "write colours as tokens, never as literal values", is the rule that makes themes possible, and `grep` can check it. On 2026-08-10, text became unreadable in light in 14 places because literal colours in the old layer were not swapped.

Two further things are affected by the theme, and both are already inside the rules of §4 and §6:

- Effects that only mean something on a dark ground, such as scanlines, grain, vignettes, and glows, are toned down or removed on the light side (scanlines .035, grain .018, no vignette). Not adding new effects is the rule of §6
- In light, amber is close to the ground in luminance and becomes unreadable as text. Using amber for attention and selection surfaces, not as a text colour, is the rule of §4. Current state: the ORD / RX keys, the selected tab, and the SPAWN label are amber text, which the light side keeps readable by swapping in a darker amber

After changing CSS, regenerate and check the theme manifest. The cockpit matches a digest of telemetry's CSS before applying a theme, so a stale manifest means standalone dark looks fine while the embedded theme is refused:

```bash
python3 scripts/dashboard_theme_manifest.py --write
python3 scripts/dashboard_theme_manifest.py --check
```

The mechanism is described under [Theme axis bridge in Dashboard](dashboard.en.md#theme-axis-bridge).

## 10. How to add UI

1. State in one line which level (§5) the element belongs to
2. Compose only from the tokens (§4) and the two faces (§3). No new hue, no new face, no literal colour
3. Never convey state by colour alone (§6). Use motion only for activity and attention, and stop it under reduce
4. Make bands collapsible and never push agent cards down (§5)
5. Attach a time to any stale value. Say "unavailable" or "not usable" in words (fail-closed copy)
6. Chrome labels are short English capitals (`USAGE · LEFT`, `WAITING FOR UPDATE`). Agent-generated content keeps its original locale
7. Nothing below 9px
8. To remove something, sink it (opacity) rather than delete it. History and lineage are valuable because they do not disappear. The history range and dense mode are a different thing: they decide what is in view, not what exists

## 11. Checks before a change

Before a visual change goes into a PR, look with your own eyes:

1. Screenshots in both dark and light with no unreadable text. Light comes from the console via `window.AgentStackColorTheme.apply({preference: 'light', resolved: 'light'})` (§9); the final check embedded in the cockpit is the maintainer's
2. With `prefers-reduced-motion`, new motion stops and the main states read from text and shape
3. In a 1440×900 viewport (the maintainer's reference; check standalone), with bands collapsed, the first agent card's name and current line fit in the initial screen
4. New CSS contains no literal colours, no `Chakra Petch`, and none of the old names `--cyan` `--bone` `--void` `--line` (check with `grep`)
5. The degraded states (no agents, empty mail, missing portrait, provider unreadable) are not blank
6. Any overlay or control you added opens, operates, and closes with `Esc` from the keyboard alone (existing bays are clickable divs with no keyboard reachability yet; this is an acceptance condition for new work, not a guarantee about the existing screen)
7. After changing CSS, run `scripts/dashboard_theme_manifest.py --write` and `--check`, and include the manifest diff in the PR (§9)

## Related documents

- Cockpit [DESIGN.md](https://github.com/gyroid-eth/orrery/blob/master/docs/DESIGN.md) (parent: concept and cockpit-side layout)
- [Dashboard](dashboard.en.md) (features and operation)
- [Configuration](configuration.en.md) (`AGENTSTACK_*` environment variables)
- [CONTRIBUTING.md](../CONTRIBUTING.md) (how to send a PR)
