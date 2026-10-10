# Documentation index

[日本語](README.md) · [README](../README.en.md)

Start with **Your first flight → help map → Full tour** on screen, from cockpit’s `Settings → Getting started`. This index is for install/update, troubleshooting, and detailed references when you need them.

Explore Telemetry’s own controls with `HELP MAP` in its header (beside `SETTINGS` in NETWORK). It annotates DECK / NETWORK controls, whether Telemetry is standalone or embedded in cockpit. In an agent detail panel, choose the panel’s own `HELP MAP` to annotate that panel’s controls.

Overview: the cockpit is the screen for operating terminals; Telemetry is the dashboard and agent-graph screen. The cockpit can embed the Telemetry screen.

![The cockpit is the screen for operating terminals; Telemetry is the dashboard and agent-graph screen. The cockpit can embed the Telemetry screen. Agents run in tmux, are visible from both screens, and talk through Mail](https://raw.githubusercontent.com/gyroid-eth/orrery/master/docs/images/cockpit_telemetry_relation.svg)

## Start here

- [ORRERY cockpit](https://github.com/gyroid-eth/orrery/blob/master/README.en.md#quick-start) — Install both with one line and start with the on-screen first-flight guide.
- [Standalone getting started](getting-started.en.md) — Audience, terms, standalone install, agent startup, and an end-to-end Mail check.

## Use the screen

- [Dashboard](dashboard.en.md) — Reference for DECK/NETWORK, history, RESUME/EXIT, REPLAY, and details.
- [Delegation and child agents](delegation.en.md) — ORRERY /delegate, differences from native children, and tool selection.
- [Launchers and identity](launchers.en.md) — agent-start, names, resume, /delegate and /log, and notification flow.
- [Using Obsidian](obsidian.en.md) — Store logs, paper notes, and tasks in a vault and Daily Note.

## Install and update

- [Update](install.en.md#upgrade) — Update while keeping existing settings.
- [Installation](install.en.md) — Requirements, standalone install, verification, upgrade/uninstall, and migration.
- [Configuration](configuration.en.md) — Look up AGENTSTACK_*, storage/exposure, model, and child settings.
- [Reusable Codex plugin lifecycle (Japanese)](codex-plugin-lifecycle.md) — Shared operations, scoped hook trust, and isolated verification.
- [Codex App integration](codex-app.en.md) — Add Desktop tasks/subagents as an optional provider.
- [Antigravity / Gemini](antigravity.en.md) — Add an optional provider and understand authentication and feature boundaries.
- [Persistent agents](persistent-agents.en.md) — Enrollment, interactive/headless startup, and credential recovery.
- [Deterministic daemons](daemon-agents.en.md) — Dedicated identity, canonical binding, notification waits, and shutdown.
- [Mail service updates](agentstack-mail-update.en.md) — Operational procedure to verify and replace a running Mail build.

## Windows and WSL

- [WSL2](install.en.md#installing-on-windows-wsl2) — Install inside Ubuntu for the standard Windows route; open it in a Windows browser.
- [Native Windows helper (experimental)](windows-local.en.md) — Community Mail/dashboard startup, separate from the standard WSL2 install.
- [Native Windows Codex launcher (experimental)](windows-codex-launcher.en.md) — Launch a preregistered child using a dedicated Windows tmux server.
- [Native Windows spawn (experimental)](windows-spawn.en.md) — Read native NEW AGENT name-catalog retrieval and the boundary that keeps SPAWN disabled.

## Troubleshoot

- [Troubleshooting](troubleshooting.en.md) — Isolate service, notification, spawn, authentication, and WSL problems.

## Design and development

- [Hooks and helpers](hooks.en.md) — Events, block/release/cleanup, and operational guard specifications.
- [API reference](api.en.md) — Routes, requests/responses, and the compatibility-generation contract.
- [Design language](design.en.md) — The reference for dashboard appearance and motion.
- [Embedded tour events](embedded-tour-events.md) — Contract for embedded Telemetry to report real actions to a cockpit checklist.
- [ORRERY Mail internals](agentstack-mail.en.md) — Bundled-package boundaries, provenance, cutover, and authority.
- [Namespace contract and migration planning](project-removal-plan.en.md) — PR1 candidate APIs and gates; defaults stay unchanged and cutover belongs to later PRs.
- [Absolute reservation candidate](absolute-reservations.en.md) — PR2 shared normalizer, authenticated isolated store, client adapters and per-lease activity/GC; legacy mode remains the default.
- [Candidate Mail and delivery state migration](namespace-state-migration.en.md) — PR3 isolated snapshots, persistent stores and phase recovery; activation stays false.
- [Global server S1 preparation](global-server.en.md) — Isolated HTTP registration/reconnection, inbox, enrollment and authority epoch; legacy mode stays default.
- [Isolated global messages and signals (S2b)](global-messages.en.md) — Eight tools, DB attachments, pure inbox, and signal reconciliation; legacy remains default.
- [Global runtime client PR4a preparation](global-runtime-client.en.md) — S1 registration, recovery, await and dedicated-HOME profile reconnection.
- [Mail update design](agentstack-mail-update-design.en.md) — Rationale and open decisions; use the update procedure for operations.
- [Mail performance gate design](agentstack-mail-performance-gate.en.md) — Design-only benchmark and adoption criteria, not a deployed benchmark procedure.
- [Claim/enrollment design](agentstack-mail-claim-enrollment-design.en.md) — Enrollment design supporting the product-decision ledger.
- [Third-party components (Japanese)](third-party.md) — Licenses and provenance for Mail, provider logos, and portraits.
- [Development entry](../CONTRIBUTING.md) — Build, validation, and contribution procedures.

Documents without a translation are labelled with their original language. Images/GIFs accompany their articles. From the repository root, run `python3 scripts/check_docs_index.py` to check index coverage.
