# Versioned schemas

The repository-level canonical files are mirrored here for component export:

- `schemas/runtime-event-v1.json`
- `schemas/binding-record-v1.json`
- `schemas/migrations/001_delivery_state.sql`

The repository test suite compares every mirror byte-for-byte with its root
canonical file so this self-contained integration cannot silently drift.
