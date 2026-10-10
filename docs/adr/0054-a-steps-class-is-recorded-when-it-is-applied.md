# A step's class is recorded when it is applied, and an ahead database may be allowed through by class

`require_applied()` and `up()` refuse a database ahead of the checkout unless `allow_ahead=True` (ADR-0038). A rolling deploy needs the flag, but `True` admits any distance and any kind of migration: a three-release rollback, a stale worker, a contract step the old code cannot live with. The application's real contract is narrower, "ahead only by migrations the previous release can run beside", and only ferro can read that off the migrations.

We decided that every step has a **class**, `expand` or `contract`, decided once, and that `allow_ahead="expand-only"` admits an ahead database only when every step record the checkout lacks is `expand`:

```python
await ferro.migrations.require_applied(allow_ahead="expand-only")   # or up(); CLI: --allow-ahead=expand-only
```

The class is read from the planner's op verdict (ADR-0050) for a generated step: any op that `drops_data`, `fails_on_rows` or `demands_values`, any rename (column, table, enum type or label), any `NOT VALID` constraint (the add-constraint step and every staged constraint: it refuses writes old code may make), a unique index step, a validate step or a type change makes the step `contract`; adding a table or a nullable column, `DROP NOT NULL`, a plain index, dropping an index, check, foreign key or unique, a label addition, every data step and guard step, and a SQLite rebuild carrying only such ops are `expand`. The rule is "any step that can make an old release's write fail is `contract`". It is a **write** notion: `expand` means the previous release's writes keep succeeding beside the step, not that its reads only meet values it knows. A label addition is `expand`, and the previous release reading a row that holds the new label gets the raw value (hydration bypasses validation, I-2); when a new label is first written is the application's law, add in one release and write in the next, and the deploying guide says so beside the rule. A hand-written SQL step is `contract` unless its file carries `-- ferro: expand`, written by its author beside the SQL where review sees it.

The class is **recorded in the step record at apply time** (by a run, a baseline or any other record writer, one computation in the core) and read from the records, never from files: in a rollback the old checkout does not have the files of the migrations it is ahead by. The generator writes no class header into generated files, since that would change every applied file's checksum (ADR-0030) and force a `rerecord` everywhere; the run computes it from the plan. `status --steps` prints it, and `DatabaseAheadError` gains `.contract_steps` (`NNNN_<name>:NN`), its message naming the first.

The column is an **additive** change to the tracking table, not a reshaping, so `format` stays 1 and the format table gains a `minor` (ADR-0030 as amended): minor 1 is the class column. Ferro 0.22's reader refuses any `format` above 1 and never selects `minor`, and every reader and writer names its columns, so a 0.22 runner reads, inserts and re-records beside the new nullable column unchanged, leaving its records without a class; the release that upgrades ferro therefore rolls back to its predecessor. A newer ferro's first mutating verb on a lower minor adds the column under the run lock and records the minor; every mutating verb classifies any record without a class, from the directory's files when the migration is present (both snapshots are there, so the verdicts are computable) and `contract` when it is not, since an ahead record of unknown class is unsafe. Readers (`status`, `require_applied`) never write and treat a missing class as `contract`.

## Considered options

- **A count bound (`allow_ahead=1`).** Rejected: it matches no compatibility law and admits a one-migration contract.
- **Record the step's headers and let the bounded check interpret them.** Rejected: renames are not headers, and a reader interpreting headers is a second decision about the planner's meaning (ADR-0052).
- **Read the ahead migrations' files.** Rejected: the files are exactly what a rolled-back checkout lacks.
- **A format bump (format 2) that an older ferro refuses.** Rejected: 0.22's shipped `check_format` refuses any `format` above 1, so the release that upgraded ferro could not roll back to its predecessor, the one rollback the two-release contract (ADR-0040) exists for. The column is additive and 0.22 already tolerates it, so no forward-compatible reader has to be built either; `format` is reserved for a reshaping.
- **Classify every pre-upgrade record `contract`.** Rejected in favour of classifying from files when present: a database that has every file is the common case and should answer at once.

## Consequences

- `allow_ahead="expand-only"` is answerable only after the first post-upgrade run on that database; the docs say so.
- The release that upgrades ferro rolls back to its predecessor on 0.22. A record a rolled-back 0.22 writes has no class and is `contract` until the next newer-ferro run classifies it.
- The deploying guide's rolling-deploy recipe shows `"expand-only"`, with `True` as the unbounded form for a fixed old version beside a new schema.
- `Headers::parse` learns `-- ferro: expand`; an older ferro refuses a file carrying it, as it refuses any unknown header.
