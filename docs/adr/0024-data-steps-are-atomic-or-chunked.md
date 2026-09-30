# Data steps are atomic or chunked, and the runner owns the chunk loop

A data step in an in-house migration is either **atomic** (one transaction; the step's tracking record commits inside it) or **chunked** (the step declares one historical-model query and a batch size; the runner pages it with keyset `after()`, opens one transaction per batch, and commits the cursor in that same transaction). We decided there is no third shape: a `transactional=False` step with a developer-owned loop was rejected because it is neither atomic nor resumable — the runner cannot record progress it cannot see, and a crash mid-loop leaves half a backfill with no cursor. Atomic is the default, matching every ORM-bundled migration tool. A DDL step that must run outside a transaction (`CREATE INDEX CONCURRENTLY`, SQLite's `PRAGMA foreign_keys` around a table rebuild) declares a ferro-owned header directive in its file; the runner then wraps nothing, the file may hold its own `BEGIN`/`COMMIT`, and the step must be safe to re-run from its first statement.

## Considered options

- **A boolean `transactional` flag with a developer-owned chunk loop.** Rejected: see above. It is the "best-effort" shape I-6 rules out.
- **Statement-level resume for no-transaction steps** (Atlas's `PartialHashes`). Rejected: the generator emits one idempotent statement pair per no-transaction step, so re-running from the top is exact and needs no second checksum vocabulary.
- **sqlx's bare `-- no-transaction` marker or a file-name suffix.** Rejected in favour of a ferro-owned header line that is checksummed with the file, written by the generator, and can grow when online-safety preambles are decided.

## Consequences

- The tracking table carries a cursor and a row count per step, written on the batch's connection inside the batch's transaction.
- On SQLite every batch transaction is `BEGIN IMMEDIATE` (a deferred read-then-write transaction is the `SQLITE_BUSY` trap), and the run lock cannot be one long transaction, because a chunked step commits many times inside a run.
- Two independent backfills are two data steps, each with its own cursor. A whole-table invariant that must hold at every commit is an atomic step over both models and accepts the lock window; the migration system does not pretend a chunked backfill can keep such an invariant.
- The generator scaffolds a chunked step only when the schema change demands values (a required column with no default; an expand/contract split); the template body is refused at load until written, never a stub that runs and does nothing.
