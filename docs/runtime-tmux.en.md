# Shared boundary for tmux consumers

This draft shares legacy transport resolution and target protection. It adds no public Mail tool or dashboard API. Legacy remains the default and activation remains false. New global launches on a dedicated server are not enabled yet; global cockpit terminal/protocol migration remains a later step.

## CLI and schema

The entry is the existing `agentstack-runtime-client`. `bin/lib/runtime_tmux_contract.json` defines the JSON contract, validated by the existing `schema_contract.py`.

| Command | Input | Output / use |
|---|---|---|
| `tmux-capabilities` | None | `contract=orrery-tmux-target-v1` and `legacy-transport` / `legacy-target-admission` / `legacy-target-diagnostics` / `legacy-window-target-admission`. Do not infer capabilities from a version string |
| `tmux-legacy-transport` | None | Verified socket, tmux argv prefix, server PID and environment removals, pinned at consumer startup |
| `tmux-legacy-target` | `--session NAME` / `--pane %N` / `--window @N`, and `--operation capture\|attach\|input\|split\|close\|kill-session\|signal` | Admitted panes/PIDs/start times and a digest. Refusals return exit2 and a fixed reason; the consumer performs no side effect on that target |
| `tmux-diagnostics` | None | Placement completeness and client_name / instance / check identifying repairs, from the same root reader; opens no socket |

A default legacy startup without a server returns only its destination and a null server_pid. It creates no socket, server or mutex; target actions remain unavailable until an actual server is verified.

All commands are read-only. They accept no arbitrary socket/root argument, token or substitute for owner authorization. `environment_unset` is a delta removing `TMUX` and `TMUX_PANE`; output never contains inherited credentials or the full environment. Global owner target/attach/compact/end require separate authorization and are not provided by this capability.

Consumers pin socket_path / server_pid / argv at startup and repeat admission immediately before each operation. Because the CLI resolves from its environment, consumers must validate the reply schema and compare the returned transport to the startup pin before any side effect. The shared check_pinned_transport performs this comparison. Only an initial null server_pid may be pinned once to the first verified PID at the same socket/argv; later changes are refused. This pin is memory only. Append operations to the returned argv prefix and apply the environment removals. Retain explicit `-S`; never return to bare name targeting. Session destruction checks every constituent pane. A pane number or name prefix alone does not prove membership.

## Legacy endpoint and target protection

The default endpoint follows `TMUX_TMPDIR` and UID. An inherited custom `TMUX` must match a UID-owned socket in a private directory and the actual server PID. A new global fixed-directory endpoint is refused as a legacy endpoint with `GLOBAL_TMUX_LEGACY_CONSUMER_REFUSED`. Standard tmux0660 sockets are accepted only beneath a same-UID0700 parent that prevents traversal by others. Existing sockets are never chmodded.

The global endpoint derivation hashes canonical client home and Mail instance into a short absolute path. Epoch, TMPDIR and deep HOME do not change it. There is no socket registry, and launch does not use this function at this stage.

Installed placement paths are read through the existing hooks/project-context.sh literal reader; env.sh is never executed. Existing config/context locates client homes validated by RuntimeOperator, so wrapper home and isolated client home are not confused.

Legacy protection reads existing context/credential/launch/live truth under fixed `clients/` using the existing read-only snapshot entry. It creates no mutex, launch, relationship ledger or SQLite handle. A live global pane matching the current pane/PID/start yields `LEGACY_TARGET_IS_GLOBAL`. Any damaged, partial or unverified root refuses side effects on all targets: an arbitrary-name standalone could be in the unverified root. Healthy root information does not erase that uncertainty. Missing/older helpers use the consumer's fixed-layout negative admission; only a proven pure legacy installation retains the previous behavior.

Do not consume a refused notification as successfully delivered: retain its existing reason/retry path. When placement is complete and only a particular global target is refused, continue other normal legacy targets. Do not stop harmless roster reads globally. Use tmux-diagnostics client_name / mail_instance_id / check to locate the repair, then read agentstack-ui-client --server-config CONFIG status --client-name NAME with that config. Placement-wide uncertainty has a null client_name. Repair the root/config, verify global_complete=true, and retry the same operation. Do not automatically remove a running root or pending intent; never borrow another receipt or adopt an unknown target by name.

Socket, pane membership and PID/start are checked before and after admission. Admission and an external tmux side effect are not one atomic OS transaction. This is not an isolation boundary against a noncooperating same-UID process replacing targets. Consumers revalidate without an intervening wait and refuse changed targets.

## What a consumer accepts as an answer

Only one complete stdout JSON with the correct contract and matching command schema, rc, reason and failure_kind is an answer. rc0 means success; rc2 is a definite target/transport refusal, which negative admission must not override. TMUX_HELPER_FAILED uses rc70 / failure_kind=helper. Missing/old helpers, empty/broken stdout, and schema/rc mismatches also mean helper unavailable and use fixed-layout negative admission. Retain legacy behavior only when global absence is proved; refuse present/unknown placement. An argparse rc2 with empty stdout for an unknown command/operation is not a domain refusal.

The JSON reasons map is authoritative. transport denotes an unresolved transport, target a refused target, and helper an internal failure without an answer. signal admits a PID/PGID side effect; the helper itself sends no signal.

Placement-read timeouts, deadline exhaustion and internal KeyError/TypeError return TMUX_HELPER_FAILED rather than a corrupt-root diagnostic. Only malformed observed files or actual unknown placement produce target refusals.

| reason | failure_kind |
|---|---|
| `TMUX_DIRECTORY_UNSAFE` | transport |
| `TMUX_SOCKET_UNSAFE` | transport |
| `TMUX_SOCKET_CHANGED` | transport |
| `TMUX_UNAVAILABLE` | transport |
| `TMUX_TARGET_UNAVAILABLE` | target |
| `TMUX_TARGET_INPUT_INVALID` | target |
| `TMUX_TARGET_CHANGED` | target |
| `TMUX_OPERATION_INVALID` | target |
| `LEGACY_TMUX_TRANSPORT_UNAVAILABLE` | transport |
| `GLOBAL_TMUX_LEGACY_CONSUMER_REFUSED` | transport |
| `LEGACY_TARGET_IS_GLOBAL` | target |
| `GLOBAL_TARGET_OWNERSHIP_UNKNOWN` | target |
| `TMUX_CLIENT_HOME_INVALID` | target |
| `TMUX_INSTANCE_REQUIRED` | target |
| `TMUX_SOCKET_PATH_TOO_LONG` | transport |
| `TMUX_HELPER_FAILED` | helper |
| `GLOBAL_ROOT_UNAVAILABLE` | target |
| `GLOBAL_PLACEMENT_UNAVAILABLE` | target |

## Implementation stages

Telemetry transport/consumers and public cockpit legacy protection use separate PRs. This draft fixes their shared CLI, and cockpit fixtures deploy its actual code through the installer payload declaration. Dedicated launch is not enabled before both merges and launcher deployment support. No rollout flag or verification ledger is added.

Tests use temporary HOME, short private directories and isolated `-L` servers. The execution boundary pins the verified absolute `-S` socket. Tests never use the live default tmux server, real provider HOME or real Mail DB.

The shared helper loads code namespaces once per consumer process; restart consumers after updating it. Literal settings results are reused only while env.sh and its reader retain the same device/inode/mtime/ctime/size/mode/UID. Context/live placement observations remain fresh per call and are never stored.

The `legacy-window-target-admission` capability checks every current pane of `--window @N` through the same before/after reader. It never substitutes the original session or only the active pane. Consumers also use this boundary for window restoration.
