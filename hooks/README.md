# Agentstack Hooks

`settings.template.json` is a Tier1 safe-global-minimal Claude Code settings
fragment. The installer should replace `__AGENTSTACK_HOOKS_DIR__` with the
installed hooks directory, typically `${HOME}/.agentstack/hooks`, then merge the
entries with a JSON parser.

The template intentionally includes only safe global hooks: registration gating,
file-reservation gating, registration marking, and a no-op-safe SessionStart
title/metadata hook. It does not install `cleanup-child-agent.sh` as a
SessionEnd hook because that script can retire an agent; SessionEnd may run
during crash/resume flows, so irreversible actions do not belong there.
