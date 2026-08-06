# AgentStack Mail performance gate design

Status: design only. This document does not add a benchmark job, choose a
budget, or authorize an authority switch.

## Purpose and boundary

The Behavior differential deliberately erases measured milliseconds and the
duration-derived Rich speed icon/footer in durable Git logs. That keeps an
otherwise exact compatibility gate deterministic, but it also makes a slower
Core look equal to live. The performance gate must observe latency before that
normalization and fail independently.

The first gate covers only the existing serial success-path scenarios and the
versioned 22-tool contract. Concurrent reservations, retirement under load,
and crash/stale-signal recovery remain outside it until their pending product
decisions are resolved. It must use worker-private state reconstructed from the
checked-in frozen-live provenance; it must not call either installed service or
open an existing database.

## What to measure

Use `time.perf_counter_ns()` around the public `call_tool` boundary in a
performance-only probe. Do not parse Rich presentation text as a clock.

| Metric | Boundary | Why it is separate |
|---|---|---|
| Cold readiness | process start to server construction plus exact tool-surface enumeration | Captures import/schema/startup regressions that no tool timer sees |
| Per-operation latency | immediately before through immediately after each call in the three ordered scenarios | Attributes a regression to one contract operation while retaining SQLite, archive, Git, and signal work |
| Workflow latency | first through last operation of each scenario | Detects cumulative overhead and interactions hidden by noisy single calls |
| Emitted speed class | class derived from the unnormalized duration for each instrumented call | Directly closes the Rich speed-class blind spot |

Responses and durable state still pass the Behavior oracle. A faster run with
wrong state is not a performance pass. The probe records raw nanoseconds,
operation name and ordinal, side, repetition, run order, Python/dependency
fingerprint, source hashes, and runner identity in a JSON artifact.

## What to compare

Each measured repetition creates fresh live and Core state below the same
temporary filesystem. Run the sides in alternating order (`live/Core`, then
`Core/live`) so cache warming and host drift do not always favor one side.
Discard warm-up pairs and calculate paired `Core / live` ratios; do not compare
unpaired samples from different machines.

Two independent budgets are required:

1. A relative budget compares Core with the frozen live side in the same run.
   It detects extraction overhead even when the host is globally slow.
2. A versioned absolute Core budget protects user-visible latency when both
   sides slow together because of a dependency or filesystem change.

For each operation and workflow, report median, p95, paired median ratio, and
speed class. A blocking rule must check all three dimensions: Core may not
cross into a slower speed class than its approved budget, exceed its relative
ratio limit, or exceed its absolute p95 limit. Aggregate-only comparison is
insufficient because one slow inbox or reservation path could be hidden by
many cheap calls.

No numeric limit should be invented in the implementation PR. First collect a
calibration artifact from at least 30 paired repetitions on the chosen blocking
runner, inspect outliers, then commit reviewed values in a versioned
`performance-budget-v1.json`. That fixture pins the live/Core source hashes,
Python and dependency set, operation order, sample policy, class boundaries,
and per-metric limits. Changing a budget is therefore a reviewed product
change, not an automatic response to a red build.

## Where it blocks

Add a dedicated required PR check, `agentstack-mail-performance`, rather than
putting timing assertions in the Behavior differential. The PR check runs a
short calibrated paired batch and enforces relative plus speed-class budgets.
A longer release check runs the p95 batch and enforces the absolute budgets on
the designated target-macOS runner. Until that stable runner and its baseline
exist, the absolute lane is report-only and authority cutover remains no-go;
an `ubuntu-latest` number must not be presented as a Mac latency guarantee.

The performance job is separate from the normal test matrix so parallel load
from repository tests does not contaminate measurements. It has no network or
LLM access and never reuses a state root. Dependency/source drift, missing
samples, unequal operation traces, worker errors, or an unrecognized speed
class make the required check fail closed as an invalid benchmark rather than
silently skipping it.

## How it fails

On a valid threshold failure, exit non-zero and print the smallest actionable
table: operation, live median/p95, Core median/p95, paired ratio, speed classes,
and the exceeded versioned limit. Upload the complete raw JSON and a compact
summary as CI artifacts. Identify infrastructure-invalid runs separately from
performance regressions, but neither state is green. Retrying may gather
evidence; it must not rewrite the budget or convert an inconclusive run to a
pass.

The initial implementation is complete only when mutation tests prove that a
synthetic Core delay fails the per-operation, workflow, and class paths, and
that deleting timing samples or changing the source fingerprint also fails.
