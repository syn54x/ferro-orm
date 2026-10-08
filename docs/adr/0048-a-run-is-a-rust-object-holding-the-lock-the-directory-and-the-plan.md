# A run is a Rust object holding the lock, one directory read and the plan; Python walks its steps

Amends ADR-0028 and ADR-0029.

`ferro migrate up` against `0008_author_slug` (`01_expand.up.postgres.sql`, `02_backfill_author.py`, `03_contract.up.postgres.sql`) is still walked by a Python loop (ADR-0028), but the loop holds one object instead of re-telling Rust who is running on every call:

```python
tracked = await _core._open_tracked(name, tracking_schema, directory)
async with tracked.locked(timeout, on_wait) as run:          # records re-read under the lock
    keys = order_keys(run.migrations, run.records)           # the one directory read, held
    plan = await run.plan({"direction": "up"}, allow_ahead=False, order_keys=keys)
    for step in plan.steps:                                   # opaque step handles
        if step.data:
            await run.start(step)                             # 02_backfill_author.py
            ...                                               # Python runs up(ctx)
            await run.finish(step, ms, tx=route)              # inside the step's transaction
        else:
            await run.execute(step, on_attempt)               # 01_expand, 03_contract
```

Rust will own the connection name, the dialect, the tracking schema, the run lock, one read of the migrations directory and the plan made from it. `run.execute(step)` is to run the bytes that read hashed. Nothing is to cross back to be checked: no step JSON, no SQL text, no record, no direction. Execution is never to go back to disk. The planned step is to carry what its execution needs, all from the held read:

- the migration's first data step (the backfill a contract's recipe re-runs);
- the snapshots a data step's historical models are built from;
- for a SQLite `foreign-keys-off` step, its rebuild expectations. Each table the step rebuilds comes with its starting name and the columns its migration's two adjacent snapshots declare. `plan_run` is to read them off the step file's own bytes, lexed once. Those bytes are what runs, and an edited unfinished step may run (ADR-0030). The executor is only to compare the expectations with the live catalog.

There are two types, and a write without the lock cannot be called at all. `TrackedDatabase` reads: records, `status`, a preview plan, whether the lock is held, the held directory. `LockedDatabase` exists only inside `tracked.locked(...)`, re-reads the records once it holds the lock (ADR-0029), and alone carries `plan`, `execute` and the record transitions. `status`, `drift` and `require_applied` open a `TrackedDatabase`. `up`, `down`, `baseline` and `rerecord` lock it. `down` previews, prompts, locks and plans again.

Python never builds a step record. A data step moves its record through named transitions on the locked object (`start`, `advance`, `finish`, `fail`, `advance_revert`, `fail_revert`, `remove`), passed the in-transaction route where its work commits. Every record-writing transition, and `execute`, verifies the lock first. Where the record write sits inside the transaction that commits the work (a transactional SQL step, an atomic data step, a chunked batch), nothing commits unless this process held the lock at commit time.

The plan carries every run target and its refusal. `up`'s `through` is `Direction::Up { through }`, bounded and refused in `plan_run`. `RunRefusal::AppliedMissing` says whether `allow_ahead` alone would have let the run through (`ahead_only`), so `DatabaseAheadError` comes from one planner answer.

## The interface

```text
_core._open_tracked(using, tracking_schema, directory) -> TrackedDatabase
TrackedDatabase   .dialect  .tracking_table  .records  .migrations (the held read)
                  await .lock_held()   .status(order_keys)   await .plan(direction, allow_ahead, order_keys)  (a preview)
                  .locked(timeout_s, on_wait) -> async context -> LockedDatabase
LockedDatabase    the reads above, records re-read under the lock
                  await .plan(...) -> Plan(steps: [StepHandle], ahead)
                  await .execute(step, on_attempt)                        a SQL step, up or down
                  await .start / .advance / .finish / .fail
                        .advance_revert / .fail_revert / .remove           a data step's record
                  .plan_baseline(target)  await .write_baseline(plan)  await .remove_baseline()
                  .plan_rerecord(target, mode, order_keys)  await .rerecord(action)
StepHandle        migration  migration_name  step  file  path  checksum  data
                  nothing_to_reverse  edited  resumes  resume_cursor  rows_done
```

A preview plan's steps cannot be executed. The first write of a locked run creates the tracking tables where they are missing, so there is no separate "ensure" call. Baseline and rerecord are planned on the locked object, because they only ever run under the lock. The test-only doors (closing the lock's connection; a lock that never took the advisory lock, as a transaction-mode pooler hands back) are `_`-prefixed methods on these objects, never module functions.

## The Python side

The Python loop shrinks to match:

- `up` and `down` become one walk, `_walk(run, plan, say)`. The plan's direction decides which transition settles each step and whether the progress line reads `applied (N ms)` or `reverted`. One builder makes the historical models of migration N from the held directory, for the walk and for the order-key computation alike.
- Each verb is one public function, `(settings=None, database=None, *, using=None, url=None, …verb options)`. The CLI and the application call the same function. Its database and connection are resolved once by `Target.resolve(...)`, which owns three things: "one of `using` or `url`", the default connection, and turning a `SettingsError` into a `MigrationRefused`. `Target.open()` gives the connection name for the verb's lifetime, closing a private connection afterwards.
- `ferro/migrations/api.py` is deleted. `require_applied` moves beside `status`.
- `up` and `down` raise `MigrationRefused` for every caller, the CLI included, carrying the `RunReport` as `.report`. The CLI catches it and renders the report, with exit code 1. `drift`, `check` and `baseline` keep returning reports with `raise_for_problems()` (ADR-0045).
- Modules move, names do not: the public names and `ferro.migrations.__all__` stay exactly as documented.

## Considered options

- **A Rust object holding only the lock and the connection, with the plan staying JSON.** Here `PlannedStep` would grow every field execution needs as JSON (direction, snapshot IR, SQL bytes, rebuild expectations) and travel to Python and back. Rejected: every step would carry its snapshots through JSON, and Rust would still have to check each returned step against what it planned. The planned step does carry those facts in this decision, but it holds them in Rust, and Python sees only a handle.
- **A Python-side session over the existing calls.** Rejected: the integer lock-handle registry, the optional `lock=` and verify-as-a-caller-duty all survive it.
- **One object with an optional lock**, whose writes refuse at run time. Rejected: two types make the unlocked write impossible to call rather than an error.
- **Record transitions in Python first, moved to Rust later.** Rejected as a stop-gap (AGENTS.md I-6). ADR-0028 already gives step records to Rust.

## Consequences

- The `RUN_LOCKS` handle registry, `LOCK_IN_USE`, `_acquire_run_lock`'s `governed_schema`, every optional `lock=` and the `_execute_sql_step` cross-checks are deleted. So are the executor's directory re-reads (`adjacent_snapshots`, `step_table_renames`) and `backfill_rerun_step`'s second filename parser.
- A test that needs a held lock opens `tracked.locked(...)`. A test that needs a dropped lock closes the lock's connection through the locked object, never by patching runner globals.
- `ferro_version` on a record is stamped in Rust (`Cargo.toml` and `pyproject.toml` carry one version).
- ADR-0028 is amended: its "one Rust function" is this object's one directory read and its plan. ADR-0029 is amended: verification sits in the record writes.
