# bench/ — ORRERY Mail performance, as code

The 2026-08-13 performance numbers (send 90 ms, register 60 ms, `tools/list`
20.4 KB) were measured once, by hand, and written into a log. Nothing would
have noticed them getting worse. This directory turns the ones that can be
automated into a script, and writes down the ones that still need a human.

| Tier | What it measures | Automated |
|---|---|---|
| 1 | Per-call latency of the HTTP surface | Yes — `tier1_latency.py` |
| 2 | Notify → reply round trip between two agents, and child spawn | No — manual procedure below |
| 3 | 4-agent relay, two laps: function and performance together | No — manual procedure below |

## Tier 1 — running it

```bash
REPO=$(git rev-parse --show-toplevel)
PYTHONPATH="$REPO/packages/agentstack_mail/src" \
  ~/OSS/claude-agent-stack/.venv/bin/python "$REPO/bench/tier1_latency.py"
```

It starts its own server on an ephemeral port with a throwaway state root under
`$TMPDIR`, and never touches ports 8765 / 8770 / 7333 or `~/.agentstack`. The
run takes about 15 seconds and appends one JSON row to `results.jsonl`.

`PYTHONPATH` is not optional and not cosmetic: the venv's editable install
points at the main checkout, so without it a worktree measures the *other*
tree's code. The row records the resolved `agentstack_mail.__file__` — check it
rather than trusting the invocation.

Useful flags: `--iterations` / `--warmup` (sample size), `--commit-async` and
`--tools-log` (server configuration), `--no-append` (do not touch
`results.jsonl`), `--no-gate` (report without a nonzero exit), `--label` (a tag
stored in the row).

## The detector comes before the numbers

A successful `tools/call` body **contains the substring `"isError": false`**.
A check of the shape `'isError' not in body` therefore classifies every success
as a failure — that bug was written and hit on 2026-08-13. So the response is
parsed as JSON, and a call is OK only when the envelope has no top-level
`error`, `result.isError` is present and exactly `False`, and a payload is
extractable.

That claim is tested in the same command that does the measuring. Before the
first timing, the script issues an unknown tool call and a call with missing
required arguments, and requires both to be classified as errors, plus a
healthy `health_check` classified as success. If a planted failure is not
detected the run aborts (exit 2) rather than reporting timings taken with a
blind instrument. Any error inside the measured loop also aborts, so a failed
call can never be averaged in as a fast one.

## What the numbers are

Measured 2026-08-14 on this MacBook Pro, scratch server, 25 iterations, empty
archive, `commit_async=true` (values in ms):

| Config | register p50/p95 | send p50/p95 | reservation p50/p95 |
|---|---|---|---|
| production-shaped (tool log on) | 32 / 41 | 76 / 89 | 47 / 58 |
| tool log off | 16 / 24 | 50 / 64 | 29 / 39 |
| `commit_async=false` (git in the request path) | 217 / 252 | 310 / 344 | 259 / 271 |

The first row in `results.jsonl` (`phase5.5 baseline`) is slower than the top
line above — 39 / 89 / 55 — because it was taken while the test suite was
running. That is the point of the loose hard gate: contention moves these
numbers by tens of percent, and the gate must not care.

Two things worth keeping:

- **The scratch archive is not dramatically faster than production.** The
  expectation going in was that a new archive would be much quicker than the
  53k-file one; measured, it is the same order (send 76 ms here vs 90 ms in
  production). With the commit off the request path, archive *size* no longer
  dominates a call — so a fresh-archive number is a fair proxy for production,
  and the remaining cost is elsewhere.
- **The rich tool log costs roughly half of every call.** It is on in
  production, so the gate is calibrated with it on.

## Thresholds, and why they are where they are

Hard gate (exit 1 when exceeded), per operation: **p50 ≤ 500 ms, p95 ≤ 900 ms**,
and `tools/list` ≤ 32 KB. That is ~6× the measured p50 — deliberately loose, so
a busy laptop never produces a red run. It catches only structural collapse:
the 1.7–7 s per-call floor that a synchronous commit into a large archive
produced before `AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC` existed.

Watch band (printed, recorded, exit 0): register 120/200, send 200/320,
reservation 150/250 — roughly 3× the measured p50. This is the level that
actually detects the regression worth detecting: putting git back in the
request path lands every operation above the watch band *even on an empty
archive*, while staying under the hard gate. A run with warnings and no
breaches is the signal to go look.

Splitting the two is the honest resolution of a real tension: one number cannot
be both immune to CPU contention and sensitive to a 3× slowdown.

`tools/list` is capped because every client pays for it on connect: 19.9 KB
over 24 tools today, against a 32 KB ceiling.

## Gameability check

The exercise: find a way to satisfy the thresholds with no real work. Four were
found; three are closed in code, one is documented.

1. **A server that accepts every call and drops it** would be the fastest
   possible pass. Closed: after the loop the script asserts the work landed —
   every message is in the recipient's inbox, the canonical `.md` and the
   reservation JSON exist in the archive, and the deferred git commits actually
   arrive (polled, since `commit_async` only defers them). Any shortfall aborts
   the run.
2. **Configure the server quiet and call it fast** — turning the tool log off
   halves every number. Closed: gating requires the production profile
   (`TOOLS_LOG_ENABLED=true`, `ARCHIVE_COMMIT_ASYNC=true`). A run outside it
   reports its numbers with verdict `ungated` and can never claim a pass.
3. **Shrink the sample until the tail disappears** — with `--iterations 1`, p95
   is p50. Closed: gating requires ≥ 20 iterations, and warmup ≤ 5 so cold-path
   cost cannot be parked in unrecorded calls.
4. **Compare rows measured on different machines or configs.** Not closed by
   code: every row records host, git sha, resolved source path, and the full
   settings dict. Compare like with like; a row whose `settings` differ is not
   a data point about a regression.

## results.jsonl

One JSON object per run, appended, never rewritten. Each row carries the
timestamp (from `date`), git sha/branch/dirty flag, host, python version, the
measured source path, iteration counts, the server settings, the detector
self-test outcomes, p50/p95/min/max/mean per operation, `tools/list` size, the
effect assertions, the thresholds in force at the time, and the verdict.

Thresholds are stored in the row on purpose: when they are later changed, old
rows still say what they were judged against.

## Tier 2 — notify → reply round trip (manual)

Two agents, one message, one reply. This is the number a user feels as "how
long until the other agent answers".

Reference values, 2026-08-13:

- Warm round trip: **10–12 s**. The floor is the receiving model's generation
  time, 8–10 s of it — so this measures the transport's overhead on top of an
  irreducible cost, and single-second differences are noise.
- Child startup pipeline: **≈ 9 s** total — preregister 2.2 s, spawn 0.6 s,
  process start → first action 6.2 s.

Procedure:

1. Spawn two agents via `/delegate` (never bare `tmux`; spawning by hand loses
   the mail registration and the parent link).
2. From one, `send_message` to the other with an instruction to reply
   immediately with a fixed short string, and wait with
   `~/.agentstack/bin/agentstack-await-reply --agent-name … --from … --after-id
   <id of the message just sent> --timeout 300`. Do not hand-roll a polling
   loop: it consumes the notification signal and kills the push it is waiting
   for.
3. Record: send → reply arrival wall clock, both agents' models (the receiving
   model sets the floor), warm or cold, and the timestamps of the two rows in
   the mail DB.
4. Repeat 3 times and report the median; a single sample of a
   model-generation-bound quantity says little.

Record the model on both sides. A round trip that "got slower" after a model
switch is a different measurement, not a regression.

## Tier 3 — 4-agent relay, two laps (manual)

The integration pass: function and performance in one run. Reference:
**7 min 49 s** for two laps (2026-08-13).

Procedure: spawn four agents via `/delegate`, hand agent 1 a token, and have
each pass it to the next with `send_message`, twice around the ring.

Record, per lap and in total: wall clock; per-hop latency (send timestamp →
next send timestamp); how many hops needed a nudge rather than proceeding on
the notification alone; any message that arrived but was not acted on; each
agent's model; and whether the agents were warm or cold-started. The
per-hop breakdown is the useful artifact — a slow total is otherwise
indistinguishable from one slow participant.

Do not automate this by having an agent spawn the fleet as a side effect of a
benchmark run. It costs real tokens and real processes; a human decides when to
spend them.

## CI

Not wired in. Tier 1 needs the package venv, and a hosted runner's timing noise
would only support the hard gate, not the watch band that carries the actual
signal. The intended shape when it is added: `--iterations 20 --no-append` on
the hard gate only, treating watch-band warnings as informational.
