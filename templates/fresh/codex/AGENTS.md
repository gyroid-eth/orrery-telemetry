# ORRERY Mail: explicit fresh client

These instructions apply only with ORRERY Mail tools supplied by the prepared
fresh client. One human uses this machine and trusts their agents. The host
holds the fixed Mail instance, owner ID, and credentials. A working folder is
not a Mail namespace or identity.

## Use the provided connection

Use the actual bound tool schema. Do not register yourself, read binding/token
files, or add caller identity, instance, credentials, request UUID, project, or
path-anchor fields to tool arguments. Recipient/target ID selectors accepted
by the schema are allowed. If an embedded task says preparation is complete,
start it without a startup inbox or registration ritual.

When identity verification is needed, use the bound `runtime_status`; ordinary
Mail operations need no extra ritual. A missing, corrupt, mixed, or expired
context is a refusal, never permission to search for another owner or use old
Mail. Report the exact non-secret error and stop the affected action.

For messages, use the published recipient selectors. A unique name is accepted;
ambiguity counts retired owners too. Inspect candidates and explicitly select
an active numeric target ID; never choose by newest row, largest ID, or cwd.
Inbox fetching is a peek. Mark read and acknowledge explicitly for this owner
when intended; notification completion is neither read/ACK nor task completion.
On an uncertain write, retain its returned request ID for trusted local replay
of saved bytes. Do not send again under a new request or invent a replay field.

## Reserve before edits

Use bound `reserve_files` before every shared edit, with the actual absolute
target paths and `ttl_seconds` at least 900. Renew while working and release
when finished. Reservations apply across working folders; no project input is
needed. The current owner must have an active lease covering each literal edit
target. An old grant receipt, conflict-free check, or intersecting directory is
not edit permission. Unknown filesystem rules or transport failures block edits.

The APFS Japanese character profile is limited; follow
`__AGENTSTACK_HOME__/docs/absolute-reservations.md`. Do not rename work, move it
to ASCII folders, disable guards, or alter filesystem settings to bypass a
refusal. MultiEdit/NotebookEdit currently refuse; a rejected multi-file Codex
patch can retain an earlier lease extension without authorizing any edit.

## Handoffs and failures

Send pointers and concise findings through ORRERY Mail. Do not drive another
agent through terminal keystrokes or a built-in collaboration substitute. A
complete notification body needs no fetch; fetch truncated notifications before
acting. Do not poll inboxes in a loop or read mailbox directories as a substitute.

For an automatic Claude/Codex child, follow the fresh child procedure in
`__AGENTSTACK_HOME__/docs/fresh-global-update.md`. Use the existing preregister
and spawn scripts with a stable child-client slot and task file. They prepare
a separate owner/context and reuse the family's already prepared provider HOME;
they never approve hooks. Preparation/accepted handoff does not prove native
readiness. Preserve an unknown attempt and report it instead of starting twice.
Managed dsh/Hermes/Local, App and dashboard child routes remain unconnected at
this stage. Do not substitute old global launch, built-in subagents or a new
identity when an unsupported route or missing preparation refuses.

Never read `storage.sqlite3` directly, including archived copies. Read old
Mail only through `agentstack_mail.archive_cli` on an explicitly finalized
copy. Carry selected output forward as new Mail/notes; reserve a note before
writing it. Old credentials, unread state, leases, and bindings are not inherited.

Follow the shared instruction consent contract: review changes to existing
content with the human; new files follow that contract's separate rule. Human
approval governs hook trust, deployment, and cutover.
Codex hooks require prior human review; ordinary launch only checks existing
trust. Do not approve `/hooks`, manufacture a hook plan, or rewrite a trust
receipt yourself. On an OS/tool approval refusal, report the verbatim refusal
to the parent and stop that action. Do not change settings or checksums to get
past it. See `__AGENTSTACK_HOME__/docs/fresh-global-update.md` for the
agent/operator procedure and outstanding acceptance boundaries.
