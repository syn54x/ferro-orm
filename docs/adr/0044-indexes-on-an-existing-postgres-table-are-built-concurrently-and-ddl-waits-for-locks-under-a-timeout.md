# Indexes on an existing Postgres table are built concurrently, and DDL waits for locks under a timeout

`Post` gains a unique and an index over columns its populated table already has:

```python
class Post(Model):
    slug: str = Field(unique=True)
    views: int = Field(index=True)
```

Until this decision the generator wrote both into the schema step, inline: `CREATE UNIQUE INDEX "uq_post_slug" …` holds a `SHARE` lock for the whole build, so every write to `post` waits for the length of it. On Postgres the generator now writes one **index step** per index, each a no-transaction step (ADR-0024) that builds concurrently:

```text
migrations/0012_post_slug_views/
  01_schema.up.postgres.sql          (nothing for these two; absent when the migration has no other DDL)
  02_uq_post_slug.up.postgres.sql    -- ferro: no-transaction
                                     -- ferro: data-dependent
                                     DROP INDEX CONCURRENTLY IF EXISTS "uq_post_slug";
                                     CREATE UNIQUE INDEX CONCURRENTLY "uq_post_slug" ON "post" ("slug");
  02_uq_post_slug.down.postgres.sql  -- ferro: no-transaction
                                     DROP INDEX CONCURRENTLY IF EXISTS "uq_post_slug";
  03_idx_post_views.up.postgres.sql  -- ferro: no-transaction
                                     DROP INDEX CONCURRENTLY IF EXISTS "idx_post_views";
                                     CREATE INDEX CONCURRENTLY "idx_post_views" ON "post" ("views");
  03_idx_post_views.down.postgres.sql
```

`CONCURRENTLY` builds in two passes under `SHARE UPDATE EXCLUSIVE`, so writers keep going; it costs about twice the build time and refuses to run inside a transaction. The end state is byte-identical to the inline form.

## A unique is an index

The ticket assumed the online unique was two statements, `CREATE UNIQUE INDEX CONCURRENTLY` followed by `ALTER TABLE … ADD CONSTRAINT … UNIQUE USING INDEX`. Ferro has never created a unique *constraint*: the reconciliation pass writes `CREATE UNIQUE INDEX IF NOT EXISTS "uq_*"` and the Alembic bridge writes `sa.Index(unique=True)`, and the snapshot, the drift check and `pg_constraint` know no constraint by that name. The second statement would have made the migrations door the one emitter that leaves a `pg_constraint` row behind, an artifact no other door produces and the drift check would have to learn to ignore. A unique index refuses duplicates exactly as the constraint would; the constraint form matters only to `ON CONFLICT ON CONSTRAINT` and as a foreign-key target, neither of which ferro uses on a unique. The online unique is the one `CREATE UNIQUE INDEX CONCURRENTLY` statement.

## Always, and for every index on a table that already exists

The criterion is ADR-0042's and ADR-0043's: every index and unique added to a table that already exists is an index step, whether or not its column is new. An index on a table the same migration creates stays inline in the schema step; the table is empty and there is nothing to block. No flag and no config key: an opt-in would make the write-blocking form the one most projects ship.

Dropping an index is the mirror. A plain `DROP INDEX` takes `ACCESS EXCLUSIVE`, waits behind every in-flight query and queues every new one behind itself, so an index the model no longer declares is dropped by an index step holding `DROP INDEX CONCURRENTLY IF EXISTS`, whose down is the build above. A changed index (a different column set, index to unique or back) is the build step as written: its first statement drops the old index under the name the new one takes. An index rename, which a column rename hint drags along, is `ALTER INDEX … RENAME TO`, metadata-only and transactional, and stays in the schema step.

## Re-runnable from the first statement

A concurrent build that fails leaves an `INVALID` index behind: present, maintained on every write, never used. `CREATE INDEX CONCURRENTLY IF NOT EXISTS` is a trap here: it sees the name, skips, and leaves the broken index in place silently. So the step's first statement is `DROP INDEX CONCURRENTLY IF EXISTS`, and the file is exact from the top (ADR-0024's rule for a no-transaction step) without the runner knowing anything about indexes. The one imperfect case is a run killed after the build finished and before its record was written: the re-run drops a valid index and builds it again, correct and online, wasteful on a big table, and the same crash-between-DDL-and-record window ADR-0024 accepts for every no-transaction step. The runner inspecting `pg_index.indisvalid` to decide what to drop would carry index-specific logic for one kind of step and was not taken.

The `CREATE` renders through the reconciliation pass's own index renderer in a concurrent mode: the two renderings differ by the one token (`IF NOT EXISTS` against `CONCURRENTLY`), and that is the I-1 pin.

## After the backfill

ADR-0040's expand step held a new column's index and unique. The index step takes them out of the expand and sits after the migration's data steps, before the add-constraint step: `expand → backfills → index steps → add_constraint → contract`. A backfill over a populated table writes every row, and an index on the column would be maintained on each write; building once over finished data is the bulk-load recipe. The unique step is the migration's data-dependent one: a duplicate fails it, not a backfill batch mid-stream, and the runner fills ADR-0040's message with a count of the duplicates (`post.slug has 2 duplicate values across 5 rows`); the backfill, over rows that still need a value, is re-run exact afterwards. No data step is scaffolded (ADR-0043's reasoning: ferro cannot know whether a duplicate is deleted, renamed or merged). In a migration with no data steps the index steps follow the schema step.

## An invalid index is drift

Introspection never read `pg_index.indisvalid`, and the pass's `CREATE INDEX IF NOT EXISTS` would see an invalid index's name and leave it broken. Live index introspection now carries `valid`; a declared index that exists live and is invalid is drift; the one planner emits a rebuild op for it (drop, then create) that the reconciliation pass executes and `drift` reports. The drift check already refuses on a database with an unfinished step (ADR-0043), so a leftover of a failed index step is never drift: `up` re-runs the step and clears it. Drift only ever meets an invalid index made outside the migration system.

## DDL waits for locks under a timeout, and retries

The quiet failure of online DDL is not the scan but the queue. `ADD CONSTRAINT … NOT VALID` needs `ACCESS EXCLUSIVE` for an instant; queued behind one long report query it waits, and while it waits every new query on the table queues behind it. Every DDL statement ferro executes against Postgres, in a run and in the reconciliation pass alike, now runs under a **DDL lock timeout**: `SET LOCAL lock_timeout` inside a transactional step, `SET` and `RESET` around a no-transaction one. A statement that gives up is retried, the whole step from its first statement (a transactional step was rolled back; a no-transaction step is exact from the top), up to ten attempts with the wait between them doubling from one second and capped at thirty, each attempt logged, the step record `running` throughout so `status` and a second runner see a live run. After the last attempt the step is `failed`:

```text
lock on "post" not acquired within 5s after 10 attempts; set ddl_lock_timeout under [tool.ferro] or run when the table is quieter
```

and `up` resumes it. One config key per database, `ddl_lock_timeout`, default `5s`, `0` disabling both the timeout and the retry: the timeout is the one number that depends on the workload; attempts and backoff are policy, and two more keys would be two more ways to configure the pile-up back in. The key is not `lock_timeout`, which `up()` and `baseline()` already take for the run-lock wait (ADR-0038); the two waits are different locks. It applies to DDL steps only: a data step runs the developer's queries under Postgres defaults. `statement_timeout` is not set: a `VALIDATE` or a concurrent build legitimately runs for minutes. The run lock's own acquisition is outside it (a second runner waits by default, ADR-0029). SQLite has no lock queue of this shape; its `busy_timeout` is the connection's and the key is a no-op there.

The reconciliation pass reads the same key through `FerroSettings` (ADR-0038 already has it reading the file) and executes through the same primitive: rung 2 of the ladder is a production door too, and a `CREATE INDEX IF NOT EXISTS` queued behind a long read freezes a table there just the same. The pass keeps its plain statements, since it runs inside its transaction; only the lock policy is shared.

## Where it applies

- **SQLite** builds every index in a transaction and has no online form. Under shared step numbers (ADR-0037) the SQLite rendering of an index step is the plain, atomic `CREATE [UNIQUE] INDEX` (or `DROP INDEX`) with no header, so a step named `uq_post_slug` holds that index on every dialect and the data-dependent marker sits on the same step number everywhere. ADR-0043 chose `not-applicable` for the validate step only because a step named `validate` must never hold a rebuild; nothing of the kind applies here. A SQLite-only project gets the same steps; once the table already exists an index is inline in no dialect's expand.
- **The Alembic bridge renders the plain op**, `op.create_index` / `op.drop_index`, marked data-dependent for a unique, in the revision's one transaction, where `CONCURRENTLY` could not run anyway (ADR-0042's reasoning: online shapes are a property of ferro's runner, not of the planner's op).

## Considered options

- **`CREATE UNIQUE INDEX CONCURRENTLY` plus `ADD CONSTRAINT … UNIQUE USING INDEX`.** Rejected: a `pg_constraint` row no other emitter produces.
- **`CONCURRENTLY` for the unique only, or behind a flag.** Rejected: the unsafe default is what most people would ship.
- **`CREATE INDEX CONCURRENTLY IF NOT EXISTS` as the one statement.** Rejected: skips over an invalid leftover and leaves it.
- **The runner reads `indisvalid` and drops only an invalid leftover.** Not taken: index-specific logic in the runner to save one rebuild in a rare crash window.
- **Index steps before the backfill, as the expand held them.** Rejected: every backfill write maintains the index, and a duplicate fails a batch instead of the unique step.
- **`not-applicable` for the SQLite rendering, the index inline in the expand.** Rejected: the step name would hold the index on one dialect and nothing on the other for no reason.
- **A per-file `-- ferro: lock-timeout` header.** Rejected: a lock policy belongs to the database, not to one file; the header vocabulary says what a file *is*.
- **`ddl_lock_timeout`, `lock_retries` and `retry_delay` as three keys.** Rejected: the timeout is the only workload-dependent number.
- **A `statement_timeout` beside it.** Rejected: kills the validations and builds this decision exists to allow.
- **Runner only, the reconciliation pass on Postgres defaults.** Rejected: rung 2 freezes production tables the same way.

## Consequences

- A Postgres migration that adds or drops an index on an existing table has one no-transaction step per index, after its data steps; `status` and `down --to` address the same step numbers on SQLite, where each holds the plain statement.
- ADR-0040's expand step no longer holds an index or unique on a table that already exists; the glossary is amended.
- The header vocabulary is unchanged; a step may carry both `no-transaction` and `data-dependent`.
- Live index introspection gains `valid`, and the planner gains an index rebuild op whose rendering is shared by the generator, the reconciliation pass and the drift check.
- `FerroSettings` gains `ddl_lock_timeout` per database; the runner and the reconciliation pass share one lock-timeout-and-retry executor primitive.
- Casebook A9 (add or drop a unique or an index) is amended: the `CONCURRENTLY` form it sketched as a possibility is the generated one, and the step sits after the data steps rather than in the expand.
