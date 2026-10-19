# Hermes child adapter spike

This is an offline feasibility probe, not a managed launcher. Existing launchers
and defaults are unchanged. The tested Hermes revision is
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

Run the small dependency-free checks with:

```sh
python3 -m unittest discover -s tests -p test_hermes_child_spike.py -v
```
