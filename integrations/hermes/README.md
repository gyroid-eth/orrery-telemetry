# Hermes child adapter spike

This directory contains an offline feasibility probe and an operator-owned Mail
pilot adapter. Existing launchers and defaults are unchanged. The tested Hermes revision is
`d923a5eb487a17c0a68801bba9ef0f56c81040a8`.

`offline_probe.py` takes `--source`, `--site` and `--output` as explicit absolute
paths. Run it with the installed Python interpreter, `-I -B`, an empty inherited
environment, a private persistent `HOME` and `HERMES_HOME`,
`PYTHONDONTWRITEBYTECODE=1`, `HERMES_DISABLE_LAZY_INSTALLS=1`, and `UV_OFFLINE=1`.
It uses a dummy credential held only in memory, refuses socket connections,
checks actual builtin writes through Hermes's dispatcher, and starts a fixture
stdio MCP server. The fixture is not ORRERY Mail and produces no Mail receipt.
SDK client construction and the real CLI are deliberately outside this probe:
client construction can import bootstrap and prepare a borrowing home's
dependencies. The probe writes its private config without credential values.

`reservation_guard.py` maps Hermes's `path` argument and `read_file`/`write_file`
names to the shared attempt/task/receipt contract. It supports exact paths and
subtree `/**` patterns; other glob patterns deny. The tests exercise missing,
expired and wrong-owner receipts, root escapes, and startup without a guard.
The guard's two-tool policy is a fixture policy, not the final policy for
Hermes memory, skill management or curator. Those capabilities must remain
enabled and confined to the profile's private home in a production adapter.

The public common contract should own backend selection, credential references,
attempt/task state, receipt validation, task completion, process ownership,
reservation release and retirement. Hermes supplies configuration projection,
in-process hooks, provider/auxiliary transport enforcement, and turn/result
translation. Auxiliary routing defaults to the primary route; Hermes's per-task
auxiliary provider settings can also project a future optional separate route.
Use Hermes's named profiles under one persistent managed root: runtime
dependencies are shared while memories, skills, config and sessions stay per
profile. Temporary storage is used only for this spike. Initial managed policy
must pin cross-profile reads to the current profile and constrain external skill
directories, skill creation and trusted project directories to that profile.

Outstanding acceptance work includes the real product proxy handshake and Mail
receipt, installed CLI task-to-exit, provider/auxiliary egress and retry limits,
shared dependency startup without installation, and owned process cleanup.
Provider credentials must not enter shell-hook children. Fail-open observer
hooks cannot enforce the provider egress contract.

`runner.py` offers separate `init`, `preregister`, `configure`, `probe`, `start`,
`status`, `stop` and `retire` commands, each taking `--state`. `init` also takes
`--config` and `--profile strata|deepseek`. The private operator config contains
absolute product/interpreter/worktree/managed-root paths, the canonical parent
and connection references, and readonly profile template paths. Credentials are
represented by the template's `key_cmd`; literal keys are never copied.
Keep that machine-specific config outside the repository.

The pilot calls the product preregistration helper with `program=hermes`, uses
`child_tools.py claude-config` and its unchanged `run-mcp.sh` proxy definition,
and compares remote `whois.program` with `hermes`. Local proxy `PROGRAM` remains
the generated value. Its official-SDK preflight distinguishes schema listing
from backend authentication. The owned tmux pane selects an explicit profile
and receives one fixed initial Mail task; no task text is evaluated by a shell.
Main and every auxiliary task use the selected named provider with empty
fallback chains. Memories, skills, review and curator remain enabled.

Only the exact parent message is exposed through MCP. An in-process plugin
records its structured product receipt, rejects an automatic second send and
cross-profile selectors, and confines explicit skill paths to the profile.
Profile paths and configured shared skill directories are checked before start.
The supervisor passes a fresh environment, sets offline package preparation and
lazy-install suppression, bounds the process group and records its exit.
Dependencies in the separate managed root must already be prepared by the
operator. These flags do not certify every bootstrap recovery path as inert.

`status` accepts only a matching verified Mail receipt. Exit zero and model prose
do not count; `full_managed_task_accepted` stays unknown. `retire` first requires
observed owned-group absence, invokes the product cleanup hook, and leaves its
receipt unconfirmed until the parent's bound `whois` confirms `retired_at`.
The Mail-only pilot takes no file reservations. Full managed reservation gates,
provider egress enforcement and the five-condition completion flow remain
separate acceptance work. This pilot has only fake-helper/stdio acceptance;
real registration, provider use, Mail and cleanup require the operator's run.

Run the focused dependency-free checks with:

```sh
python3 -m unittest discover -s tests -p 'test_hermes*.py' -v
```

Set `ORRERY_HERMES_TEST_PYTHON` to the installed dependency interpreter to include
the offline SDK stdio fixture; also set `ORRERY_HERMES_TEST_SOURCE` to the tested
checkout to exercise real Hermes discovery and tool dispatch through that fixture.
Neither fixture makes a provider request or connects to a real Mail backend.
