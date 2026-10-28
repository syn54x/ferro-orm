# Schema snapshots stay readable across IR versions

Every in-house migration stores a schema snapshot: the declared SchemaIR modelset at generation time, in canonical JSON, with its `ir_version` and the checksum of the previous migration's snapshot. The generator diffs the current models against the head snapshot and never a live database, so a project's chain holds snapshots written by every ferro version it ever ran. We decided that every `ir_version` ferro has shipped stays readable by the generator and the runner for good, pinned by the golden IR vectors, and that the parent link is the checksum of the parent file as stored, not a fingerprint recomputed from models — so a serialization change in a ferro upgrade neither breaks the chain nor rewrites history.

## Considered options

- **Re-snapshot on upgrade** (a command that rewrites every `ir.json` in the current version). Rejected: it edits applied migrations, which the integrity floor checksums, and it produces a chain no other environment has seen.
- **Diff against the live database** (Alembic's shape), which needs no durable snapshot format. Rejected on this map: it requires a database exactly at head to generate, and its live-to-IR conversion is lossy (defaults, user-owned objects, non-additive enum changes are invisible), so hand-repaired databases produce phantom diffs. Live state is instead consumed by the drift check, which plans the reconciliation pass against the last applied snapshot and reports the plan.

## Consequences

An IR schema bump is a compatibility commitment, not a refactor: it ships with a loader for the previous version and a golden vector for the new one.

Amended by ADR-0056 (2026-10-18): schema IR version 3 adds an optional `replaces` to the envelope, beside `parent_checksum`: the witness of a *squash* (each replaced migration's directory name, snapshot checksum and step ordinals, nesting a replaced squash's own). The chain metadata in the envelope is exactly these two keys. As with every bump, there is one writer version (`CURRENT_SCHEMA_IR_VERSION` is 3 for every snapshot this ferro writes), the loader reads 1 through 3 and refuses a v1 or v2 envelope carrying `replaces` as it refuses a v1 carrying a hint, and a v3 golden vector is shipped. A squash is planned between two stored snapshots and never reads the models, so it is the second generator door this ADR's "never a live database" covers.

Amended by ADR-0058 (2026-10-28): schema IR version 4 adds an optional `generated` to a column: a generated column's value expression, a tagged tree of `column`, `literal`, `binary`, `negate` and `func` nodes, left out for every other column so those serialize as before. `CURRENT_SCHEMA_IR_VERSION` is 4 for every snapshot this ferro writes, the loader reads 1 through 4 and refuses a v1, v2 or v3 envelope whose column carries `generated`, and a v4 golden vector is shipped.
