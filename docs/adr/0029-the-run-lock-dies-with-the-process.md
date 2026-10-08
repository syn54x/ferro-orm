# The run lock dies with the process: a session advisory lock on Postgres, an OS file lock on SQLite

One lock guards a run, and on both backends it is a lock the database or the operating system releases when the running process dies. On Postgres it is a session-level advisory lock under a ferro-owned key (derived from the schema-qualified tracking table, not sqlx's database-name key), held on one dedicated connection for the whole run. On SQLite it is an OS file lock (`std::fs::File::lock`, stable since Rust 1.89) on a sidecar file beside the database, `<database>.ferro-migrate.lock`; an in-memory database is reachable from one process only and takes an in-process lock.

Amended by ADR-0038: the auto-migrate create and reconciliation passes take the run lock too, so the two migration doors exclude each other. For that the Postgres key is derived from the governed schema (the connection's current schema), not from the tracking table's own schema.

Amended by ADR-0048 (2026-10-07): verification is no longer a caller's duty. Every method of the locked run object that writes a step record, and `execute`, verifies the lock first. For a transactional SQL step, an atomic data step and a chunked batch, that write sits inside the transaction that commits the work, so nothing commits unless this process held the lock at commit time. A no-transaction step, whose statements commit one at a time, gets the check before it starts.

The SQLite half has no precedent. Of twelve tools read at source (2026-10), eight take no cross-process lock on SQLite (Alembic, Django, Flyway, golang-migrate, goose, Rails, dbmate, refinery), two use a lock row (Liquibase, EF Core), Atlas writes a timestamped marker file, and Prisma sets `locking_mode=EXCLUSIVE`. The eight get away with nothing because each of their migrations is one transaction that also inserts its version row: two racing runners serialize on SQLite's writer lock and the loser fails on the primary key. Ferro cannot copy that, because a chunked data step commits once per batch and a no-transaction step has no wrapping transaction (ADR-0024). A second runner that finds a started record for a half-finished backfill must resume it if the first runner was killed and stay out if it is alive, and the record looks the same in both cases. Only a liveness signal tells them apart.

## Considered options

- **No lock, transaction-only protection** (the status quo). Rejected: see above; it is sound only when a migration is one transaction.
- **A lock row, or the step's started record as the lock.** Rejected: a killed process strands the row. Liquibase needs `release-locks`; EF Core's documented fix is `DROP TABLE "__EFMigrationsLock"`. A staleness timeout is a guess a long backfill outlives.
- **Atlas's expiring marker file.** Rejected for the same guess: it blocks after a crash until expiry and admits a second runner when a live run outlasts it.
- **Prisma's `locking_mode=EXCLUSIVE`.** Crash-safe, but it locks every other connection out for the whole run, readers included, and would force every step onto one connection while data steps run through ferro's pool.
- **sqlx's `lock()` on Postgres.** Rejected: its key is shared with every sqlx-based migrator on the same database name, so an unrelated service's `sqlx migrate run` and a ferro run would block each other.

## Consequences

- A second runner **waits** by default, says at once that it is waiting for another run, and re-reads the applied records after acquiring; a lock timeout bounds the wait. A rolling deploy whose pods all run `ferro migrate up` therefore does not crash-loop.
- On Postgres the lock is **verified, not trusted**: after acquiring, and before each step and each batch, the runner checks on the lock connection that its session still holds the lock. A transaction-mode pooler fails the first check and the run is refused ("migrations need a direct or session-mode connection"); a lock connection dropped mid-run aborts before the next step or batch, and the next run resumes from the recorded position.
- Every batch's write transaction on SQLite still opens with `BEGIN IMMEDIATE` (ADR-0024); the file lock excludes other runs, not the application's own writers.
- The file lock is as reliable as SQLite's own locking, which is to say not on network filesystems; ferro makes no promise there that SQLite does not.
- Every mutating verb takes the run lock; `status` takes none. A command acts on exactly one database (ADR-0036), so a run never spans two.
