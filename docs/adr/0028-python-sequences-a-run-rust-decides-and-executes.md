# Python sequences a run; Rust decides and executes it

`ferro migrate up` against a migration holding `01_add_column.sql`, `02_backfill.py` and `03_set_not_null.sql` is walked by an async Python loop. For the two `.sql` steps the loop calls into Rust, which runs the file and writes its record; for `02_backfill.py` it swaps in the historical models and awaits the step's `up(ctx)` directly. Everything that is a *decision* stays in Rust, in one place: reading the migrations directory, hashing the raw bytes, validating checksums and snapshot fingerprints against the applied records, refusing a broken chain or an out-of-order migration, and returning the ordered list of pending steps. Rust also owns every effect on the database that is not the data step's own queries: taking, verifying and releasing the run lock, executing SQL steps, and reading and writing step records. The Python loop decides nothing; it walks the list Rust returns.

We chose this over a Rust-owned loop because ferro's bridge runs one way: Python awaits Rust futures, and nothing in Rust awaits a Python coroutine. A data step is ordinary ferro code, and `transaction()` finds its pinned connection through a Python context variable. A Rust loop would have to grow "Rust awaits Python, carrying the caller's event loop and context variables" for exactly one caller, and get cancellation right across it.

## Considered options

- **Rust owns the loop and calls into Python for data steps.** Rejected: a new FFI direction for a single caller, with context-variable and cancellation propagation as its failure surface. One place would hold the lock, but the lock is held on a Rust-owned connection either way.
- **Python owns the loop and the decisions.** Rejected: pending-step planning, checksum comparison and the refusals would then exist beside the Rust planner the generator and the drift check already use; that is a second copy of a decision.
- **sqlx's `Migrator`.** Unusable: it is a closed SQL-only loop that writes its own one-row-per-file table (see `docs/research/sqlx-migrate-internals.md`).

## Consequences

- One Rust function takes the migrations directory and the applied records and returns the pending steps or a refusal. It is the only reader of the directory, so the bytes hashed are the bytes planned; a `.sql` step executes the bytes that were hashed, and a `.py` step's loaded source is checked against the planned checksum before it runs, so a file edited mid-run is refused.
- The runner applies only a contiguous run of migrations after the last applied one, after verifying the on-disk snapshot chain. A pending migration numbered below the highest applied one is refused with no override; the error names both and says to regenerate the stray one at the head.
- `transaction()` rolls back on any `BaseException`, cancellation included, for every ferro user; today it catches only `Exception`, so a cancelled task skips both commit and rollback. On any exit the runner rolls back the open step, then releases the run lock; the step's started record stays, so the next run resumes there.
- From sqlx ferro borrows recipes, not code: SHA-384 over raw file bytes, the no-transaction header convention, and `sqlx-cli migrate info`'s status vocabulary. It owes nothing to `_sqlx_migrations` or `sqlx-cli`, and sqlx's unused `migrate` feature is switched off.
