# Preparing global child clients and resume

For execution, proxies, window binding and delivery, see [global delivery client](global-delivery-client.en.md). This guide describes child preparation; legacy remains the default.

[日本語](global-child-client.md)

This is explicit preparation against an isolated global candidate. Legacy remains default and `activation_enabled=false`. Preparation never starts a provider; explicit execution follows the delivery guide.

## One entry and fixed layout

`bin/lib/agentstack-register.sh` is the shell registration/child/resume front door. Normal context selection belongs to `runtime_client.selected(argv)`, called through a shared loader before legacy environment. A partial install with missing helpers or an unavailable `select` operation uses the same context-presence admission: a context returns `GLOBAL_ENTRY_UNAVAILABLE`; absence preserves legacy without starting Python for selection. The loader shares one definition of that presence check. Mail-disabled skips apply after global selection and before owner material, preserving legacy hook warnings and exit codes. Missing context preserves legacy; invalid context and dangling symlinks refuse. Global never falls back to project/name/token-file discovery. Wrapper and loader are one payload; a missing loader refuses an incomplete distribution. The installer checks its presence through the shared contract-source list before deployment.

Point `AGENTSTACK_CLIENT_CONFIG` to the parent's fixed `runtime-client.json`. Children occupy another safe `client_name` immediately under the same `agentstack/clients/`. `credential.json` is the sole owner credential. `runtime/child-state.json` stores parent stable ID, task/tools and lifecycle references without copying child token or identity. Arbitrary output destinations are rejected. New identities require new roots. An existing standalone root cannot become a child (`CLIENT_ROOT_NOT_CHILD`). Existing children require the same parent and registration-intent digest; changed name/program/model/task returns `CHILD_REGISTRATION_INTENT_CONFLICT` without keeping another token or payload copy.

```bash
agentstack-preregister-child --child-client-name child_01 --name ChildAlpha \
  --program codex --model explicit-model --task-description 'Task summary' --prepare-only
bash "$AGENTSTACK_HOME/hooks/spawn_child.sh" --child-client-name child_01 \
  --embed-task --task-file task.md --prepare-only
agentstack-resume --client-name child_01 --prepare-only --detached
```

`prepared=true` means local/authentication preparation, with `runtime_ready=false`. Normal spawn/resume then enters the fixed launch handoff. A new window binds atomically to the stable agent ID, while an existing window is verified. Only trusted provider session evidence makes runtime ready.

### R3 full shell caller (integration checkpoint)

For an already preregistered fixed child with an explicitly configured intent,
the same global `spawn_child.sh` entry compares the complete caller input:

```bash
bash "$AGENTSTACK_HOME/hooks/spawn_child.sh" --child-client-name child_01 \
  --name ChildAlpha --codex --model local:example --parent ParentAlpha \
  --base mail-only --resources 'Scratch.md' --detached \
  --task-file /absolute/task.md --prepare-only /absolute/workdir
```

Full input requires name/model/task-file/cwd. Provider follows the CLI's
`--codex` / `--program` or `--provider` selection; parent comes from the fixed
parent client, and `--parent` asserts its name rather than looking up another
root/credential. Resources are repeatable comma-separated values; tools use
the existing `--tools` grammar. Clear-tools/worktree/chrome default to false,
detached retains the global default true (`--no-detached` selects false), base
defaults to default, and tools/resources default to empty. Optional `--effort`,
`--strict-output-cap` / `--no-strict-output-cap` and `--task-delivery` remain
absent unless supplied. Missing input is not backfilled from the owner. The
shared parser rejects these new fields for other operations.

The existing minimal task reference remains. A saved intent also requires
matching task path/content, parent and child-state compatibility projection.
Checks repeat under the existing child mutex and pin the digest into R2.
Source files follow configure's owner/regular/no-link/no-other-write contract
(0644 is allowed); the runtime copy remains private. A minimal reference that
omits task-file reuses the approved retained copy and delivery mode without
reopening the original. An explicit task-file still requires matching source
path/content rather than borrowing a saved copy.
Preregister and spawn remain separate. Ordinary startup does not grant native
approval; unverified CLIs remain `LOCAL_MODEL_ADAPTER_UNSUPPORTED`. This is an
input/preparation checkpoint, not native permission, ordinary detached/tmux or
nondefault-effect acceptance. Delivery-only TEST success does not prove those
gates. Legacy entry behavior is unchanged.

## Interrupted registration

Repeat the same preregister command. It reuses the saved name/token/program/model/task/binding. An unsent request creates a row. An already committed request returns `NAME_CONFLICT`; authenticated parent resolution followed by child-token whois proves ownership. Delayed requests are serialized by atomic name conflict. Conflict alone never deletes pending.

Interruptions after credential, context or metadata saving complete activation through the same pending. Transport/binding uncertainty is not foreign ownership. External operator rename/delete of an unresolved name requires pending reconciliation before retry.

Only definite foreign ownership allows `abandon-child-registration --operator --expected-digest SHA256` with the child's context. It verifies the pending digest and current owner refusal, marking that same pending terminal. It does not retire another owner or delete credential evidence. A new identity requires a new root. An existing standalone root cannot become a child (`CLIENT_ROOT_NOT_CHILD`). Network success followed by disk failure retains pending and does not claim rollback.

```bash
bash "$AGENTSTACK_HOME/bin/lib/agentstack-register.sh" inspect-child --context CHILD_CONTEXT
bash "$AGENTSTACK_HOME/bin/lib/agentstack-register.sh" finalize-child \
  --context CHILD_CONTEXT --operator --agent-id ID
```

## Hooks and end

PreToolUse reads absolute-path coverage and own ACTIVE lease expiry. The sole client threshold is `RENEW_THRESHOLD_SECONDS=600`; only leases with600 seconds or less receive an1800-second renewal. Repeated edits alone do not create receipts. A lost mutation response blocks editing; the next PreToolUse first replays the saved intent of any mutation kind with the same UUID. Only a definite rejection in the shared rejection table clears pending; an uncertain result blocks editing.

Default/explicit `warn-open` permits an initial transport outage with the existing warning/audit. Refusal uses the existing value `AGENTSTACK_MAIL_OUTAGE_POLICY=block`. Context/owner/binding/schema failures, HTTP/MCP rejection and uncertain renewal outcomes remain blocked. PostToolUse cannot undo completed edits and audits failure. Grace uses the legacy `AGENTSTACK_RELEASE_GRACE_SECONDS` setting (fallback: `FILE_RESERVATION_RELEASE_GRACE_SECONDS`), default90 seconds. Per-lease `runtime/release-debounce/<lease-id>.json` slots preserve independent files. A worker verifies generation, captured lease ID, revision and expiry, preserving renewed/reacquired leases. `AGENTSTACK_MAIL_DISABLED=1` skips hooks before context/owner material and network.

SessionEnd and cleanup use the same end operation. Another live same-ID session, or unknown ps/tmux evidence, prevents release and retirement. The last child calls server retire once, which releases ACTIVE leases. The last standalone only releases its own ACTIVE leases. Execution evidence lives in fixed `runtime/live-sessions/`; no session-specific reservation ownership table is introduced. Unavailable process birth evidence stays unknown.

Expired retained cleanup requires no pending and confirmed retirement; it removes only the fixed generated task slot. Credential, self session record and provider transcript remain.

## Legacy differences and deferred work

| Item | Legacy | Global preparation |
| --- | --- | --- |
| Registration | project/name and token-file output | fixed context/credential and stable ID |
| stdout | child name | token-free preparation JSON |
| Renewal | every edit | expiry threshold, same-UUID replay |
| SessionEnd | existing hook chain | one end operation checks other sessions first |
| Outage | default warn-open / block | same policy, distinguished from authentication refusal |
| New window / execution | existing launcher | fixed launch handoff, stable ID binding and trusted session receipt |
| Released retained format | existing readers | explicit PR7 import; no automatic conversion |

Gemini stdio uses the common proxy. Interactive Gemini roots remain unsupported until a trusted native receipt is available. Native Windows remains outside the Unix contract; WSL uses the Unix client. Managed blocks belong to PR5, UI to PR6 and cutover/released-format import to PR7.

The authoritative fixture is `packages/agentstack_mail/fixtures/global-client-4b.json`:48 source/docs,9 supplemental hits,12 indirect entries and35 acceptance cases. Implementation feedback changes v2 with threshold renewal and next-hook replay. Real HTTP fixtures await matching health/profile/binding. Related validation covers legacy, bash3.2, installer extraction and deployed bytes. Clients never open/close/hash/copy Mail SQLite files.

The [caller, interruption and acceptance tables](global-child-client-contract.md) are generated from the fixture. A failed debounce worker records `last_error` in its existing fixed release slot; an invalid fence forbids that local write. Resume and expiry purge share the end operation's liveness check. A live or unknown session prevents either action.

The resume intent is saved before unretire. After response loss, management inspect resumes the same ID/token. Expired retention refuses with `CHILD_RETENTION_EXPIRED`; purged material refuses with `CHILD_PURGED`. Explicit `cancel-child-resume --operator` returns a confirmed unexecuted preparation to common retire only after ruling out live/unknown sessions.

New global launches use the dedicated socket, runner/provider evidence and stable-ID target/replay-end recovery in the [delivery guide](global-delivery-client.en.md#dedicated-launch-and-termination-recovery). SessionEnd alone does not prove process exit; unresolved reporting preserves ownership.
