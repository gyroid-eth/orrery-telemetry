# Applicability

Use the identity, program and model from your canonical launch task or runtime
binding. Do not borrow another program's identity from shared instructions.
Work only within your assigned task. This document does not authorize a new
registration. Unresolved identity or connection: stop coordination and report
the exact failure to your parent, or the operator if standalone. Editing guards
remain in effect.

## Coordination (ORRERY Mail)

Use only the provided connection and its actual tool schema. Do not add caller
identity, project or token fields that the schema does not accept. Do not read
credentials or another agent's inbox. Do not register again, invent an identity
or switch transports to recover a failed or unknown connection.

<!-- include: ### 1. Embedded canonical task UNTIL ### 3. Raw/direct connection after shell registration -->

<!-- include: ## Canonical coordination paths are fail-closed UNTIL - Delegation is one instance of the rule. Use the `delegate` skill and its -->

## File reservations — REQUIRED before editing

Project: `__AGENTSTACK_PROJECT_KEY__`. Before a shared project edit, call
`reserve_files` with project-relative paths and `ttl_seconds` at least 600.
Use the actual schema; do not add caller or project fields. Renew long edits
with `renew_reservations` and release with `release_reservations` when done.
A conflicting reservation means stop and coordinate, not overwrite.
Reserve even when a shell write is outside the editing guard.

Your own private scratch does not require a project reservation. Shared files
outside the project are not automatically yours to edit: ask your parent
before editing an unassigned shared area. Never bypass or approve guards.

## Messaging Other Agents

<!-- include: - **Send pointers, not files.** A message body is a path plus what changed UNTIL - **Instruct children over mail only.** Use `send_message` to the child's -->

## Waiting For Replies

Use notifications or an available documented wait. Otherwise save progress and
end the turn. Do not use fetch polling, custom watchers or pane input to wait
for or deliver messages.

<!-- include: ## Reading Notifications UNTIL ## Skills -->

## Tool refusals

Stop the affected step and report the exact failure and unfinished work.
For a tool refusal, send your parent:
`MCP tool <tool-name> is blocked: <exact message>.`
If standalone or sending also fails, report to the operator and stop.
Wait for a pending approval's result; do not grant it yourself or change
settings, credentials or hook trust to bypass a refusal.
