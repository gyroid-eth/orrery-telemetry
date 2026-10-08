# Versioned schemas

The repository-level canonical files are mirrored here for component export:

- `schemas/runtime-event-v1.json`
- `schemas/binding-record-v1.json`
- `schemas/migrations/001_delivery_state.sql`

The repository test suite compares every mirror byte-for-byte with its root
canonical file so this self-contained integration cannot silently drift.

## Explicit namespace candidate

`migrations/002_namespace_candidate.sql` matches the root schema copy. It defines the isolated global delivery candidate keyed by Mail instance ID, agent ID and message ID, including lease and terminal-status constraints. The existing initializer still reads only `001_delivery_state.sql`; this file never upgrades an installed DB automatically. Conversion and recovery use the explicitly constructed [state migration candidate](../../../docs/namespace-state-migration.en.md).
