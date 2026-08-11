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
versioned 24-tool contract. Concurrent reservations, retirement under load,
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
| Emitted speed class | class derived from the unnormalized duration for each instrumented call | Reports the Rich speed-class blind spot without making a threshold-boundary flake blocking |

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
speed class. Blocking uses the relative-ratio and absolute-p95 budgets. Speed
class is report-only because a sample near a class threshold is bistable: an
operationally insignificant change can otherwise alternate the required check
between red and green. A class downgrade must be visible in the summary, but it
does not change the exit code unless a later reviewed design adds explicit
margin or hysteresis. Aggregate-only comparison is insufficient because one
slow inbox or reservation path could be hidden by many cheap calls.

No numeric limit should be invented in the implementation PR. First collect a
calibration artifact from at least 30 paired repetitions on the chosen blocking
runner and inspect outliers. The proposed PR lane uses two discarded warm-up
pairs plus 10 measured pairs for median-ratio checks; the release lane uses
three discarded warm-up pairs plus 30 measured pairs for p95 and absolute
checks.

The calibration artifact must record, for every metric and both sample counts,
the observed noise floor and the smallest regression that the batch can
distinguish at its chosen confidence. A budget may not be stricter than that
lane's measured noise floor. If the product needs to detect a smaller change,
increase the pair count or reduce runner noise before making the check
required.

Commit reviewed values in a versioned `performance-budget-v1.json`. That
fixture pins the live/Core source hashes, Python and dependency set, operation
order, warm-up and measured pair counts, noise-floor method and results, class
boundaries, and per-metric limits. Changing a budget is therefore a reviewed
product change, not an automatic response to a red build. Any change that
loosens a limit must name the metric that became slower, the observed cause,
and the measurement artifact supporting the exception; simply updating the
fixture to green a build is not permitted.

## Where it blocks

Add a dedicated required PR check, `agentstack-mail-performance`, rather than
putting timing assertions in the Behavior differential. The PR check runs a
10-pair calibrated batch and enforces relative-ratio budgets while reporting
speed classes.
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
synthetic Core delay fails the per-operation ratio and release-p95 paths, that
a class-only boundary flip is reported without changing the exit code, and
that deleting timing samples or changing the source fingerprint also fails.

## What this gate cannot detect

This first gate does not measure concurrent reservation latency or fairness
(D10), retirement while work is concurrently arriving (D11), or crash,
recovery, and stale-consumer notification latency (D12). Those are central
production conditions, not negligible edge cases; they are excluded because
this first paired gate has no deterministic contention, retirement, or watcher
recovery workload. Their product semantics are selected, but passing this gate
still says nothing about latency under those conditions.

It also cannot detect sustained throughput collapse, memory/file-descriptor
growth, long-run queue buildup, network transport overhead, optional LLM paths,
or machine-specific behavior outside the selected runners. The PR lane's
paired Linux result is relative only. Until the designated Mac runner and
absolute budget exist, neither a PR pass nor a report-only Mac sample is an
absolute target-machine guarantee. Each omitted dimension needs its own
selected-semantics load, soak, or fault gate before authority cutover.
