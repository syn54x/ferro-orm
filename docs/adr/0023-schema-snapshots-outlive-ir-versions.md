# Schema snapshots stay readable across IR versions

Every in-house migration stores a schema snapshot: the declared SchemaIR modelset at generation time, in canonical JSON, with its `ir_version` and the checksum of the previous migration's snapshot. The generator diffs the current models against the head snapshot and never a live database, so a project's chain holds snapshots written by every ferro version it ever ran. We decided that every `ir_version` ferro has shipped stays readable by the generator and the runner for good, pinned by the golden IR vectors, and that the parent link is the checksum of the parent file as stored, not a fingerprint recomputed from models — so a serialization change in a ferro upgrade neither breaks the chain nor rewrites history.

## Considered options

- **Re-snapshot on upgrade** (a command that rewrites every `ir.json` in the current version). Rejected: it edits applied migrations, which the integrity floor checksums, and it produces a chain no other environment has seen.
- **Diff against the live database** (Alembic's shape), which needs no durable snapshot format. Rejected on this map: it requires a database exactly at head to generate, and its live-to-IR conversion is lossy (defaults, user-owned objects, non-additive enum changes are invisible), so hand-repaired databases produce phantom diffs. Live state is instead consumed by the drift check, which plans the reconciliation pass against the last applied snapshot and reports the plan.

## Consequences

An IR schema bump is a compatibility commitment, not a refactor: it ships with a loader for the previous version and a golden vector for the new one.
