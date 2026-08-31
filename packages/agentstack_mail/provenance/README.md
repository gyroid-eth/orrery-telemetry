# The tracked live baseline

The comparisons against the frozen predecessor need a copy of the third-party
server as it ran at the moment of the handoff. That copy is **not distributed
with this repository**, and everything that depends on it skips.

| Artifact | Status |
| --- | --- |
| `live-head.bundle` | Not distributed. Removed before publication. |
| `working-tree-tracked.patch` | Present. The tracked dirty changes above that HEAD. |

## Why it is not here

The bundle was described as a depth-1 snapshot of HEAD `b8251c1`, and this file
warned against replacing it with a full-history bundle because that would
restore a deleted private-key blob. The warning was right and the artifact was
wrong: the committed bundle held 720 commits, that key, and the previous author
identity. A text search of this repository could not see any of it, because a
bundle is compressed Git objects.

## Why it cannot simply be regenerated

The baseline is not a published upstream commit. It is an upstream checkout
plus five local commits, the last of which removed the signing key, so no
`git clone` of the upstream project reaches it. Whoever holds that checkout can
rebuild the bundle with `git bundle create ... --depth=1 HEAD`; nobody else can,
and this repository does not ask them to pretend otherwise.

## What this means for the comparison gates

They are unavailable in the published repository. `differential_source` skips at
its single entry point rather than failing, so a comparison that cannot be run
is never reported as a comparison that passed. What survives publication are the
gates that need no predecessor: the contract tests, the decision ledger, and
restore acceptance.
