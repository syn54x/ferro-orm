# Unified column-fact derivation across declaration paths

Ferro accepts column facts from two declaration paths: `ferro.Field()` /
`FerroField` (the **ferro path**) and raw Pydantic
`json_schema_extra={"primary_key": True}` (the **raw path**). Historically each
path derived some facts under its own rules, in different modules. The
`ColumnSpec` refactor (#255) compiles every column fact exactly once in
`ferro.columns` (the Column spec — see `CONTEXT.md`), which forced the two rule
sets into one place and exposed where they disagreed.

The sharpest disagreement was **autoincrement defaulting**. The ferro path
defaulted to `primary_key AND integer-typed`; the raw path defaulted to
`primary_key` alone (the #153 fix). So a non-integer primary key auto-incremented
or not depending only on which syntax declared it. The raw-path default was not
merely redundant — it produced broken output. For

```python
class Doc(Model):
    id: str = Field(json_schema_extra={"primary_key": True})
    body: str
```

the emitter (`ferro-migrate/src/emit.rs`, which applies `auto_increment()`
whenever a column is a primary key, never gated on integer typing) rendered:

- **SQLite** — invalid DDL SQLite rejects at execution:
  `CREATE TABLE "doc" ( "body" varchar NOT NULL, "id" varchar NOT NULL PRIMARY KEY AUTOINCREMENT )`
- **Postgres** — a hard panic from sea-query, surfaced as a `PanicException` that
  crashes `create_tables()`: `not implemented: String(None) doesn't support auto increment`

We adopt one rule for both paths: **autoincrement is the explicit value if given,
otherwise `primary_key AND integer-typed`.** The same `Doc` now renders valid DDL
on both backends (`"id" varchar ... PRIMARY KEY`, no AUTOINCREMENT). Integer
primary keys — the overwhelming common case — are unchanged on both paths.

More broadly, "which fact this column carries" no longer depends on declaration
syntax: the runtime consumers that previously saw only ferro-path primary keys
(shadow-FK typing, relationship descriptors, `save()`'s id assignment) now see
raw-path primary keys identically.

## Considered options

- **Preserve both rules behind one derivation function** — rejected. The
  divergence is a latent bug generator (broken DDL, a CREATE-time panic), not a
  compatibility guarantee anyone relies on deliberately. Keeping it would make the
  single derivation site lie about being single.
- **Unify on the raw-path rule (`primary_key` alone)** — rejected. Autoincrement
  on a non-integer column is not implementable on either backend, as the panic
  above shows.
- **Unify autoincrement but keep raw primary keys invisible to the runtime** —
  rejected. "Which column is the primary key" is one fact; giving it two
  visibility rules keyed on declaration syntax is the same disease in a different
  consumer.

## Consequences

- The only models whose emitted SchemaIR changes are **non-integer primary keys
  declared via raw `json_schema_extra` without an explicit `autoincrement`**:
  their `autoincrement` flips `true → false`. This fixes the invalid-SQLite-DDL
  and Postgres-CREATE-panic behaviour above. Because the Postgres path panicked at
  table creation, no existing Postgres database can hold a live table in this
  configuration — the affected shape was previously unusable.
- **Existing databases see no migration.** The migrate planner's column diff
  (`ferro-migrate/src/lib.rs`, `diff_model_columns`) compares only storage type
  and nullability for an existing column — it never diffs `autoincrement`. So the
  flip produces zero migration operations; it is observable only in fresh
  `CREATE TABLE` output.
- `save()` no longer copies a driver-returned id into an unset non-integer
  raw-path primary key (that behaviour was gated behind the same divergence). Such
  saves now require an explicit primary-key value, matching the ferro path.
- Relationship descriptors and shadow-FK typing now resolve raw-declared primary
  keys; a many-to-many join column referencing such a model types from the real
  primary-key type instead of the string fallback.
- A model may still declare `primary_key=True` on more than one column across the
  two paths (Ferro does not validate against it). With path-agnostic resolution
  such a model now resolves its primary key by column declaration order rather
  than always preferring the ferro-declared one. This is an already-malformed
  model (two primary keys); a follow-up should add validation rejecting multiple
  `primary_key=True` columns rather than resolve the ambiguity silently.
- The transitional `declared_via` field on `ColumnSpec` existed for exactly the
  two refactor commits that preserved the old behaviour during migration; it is
  deleted here. Any future per-path behaviour split is a design smell — a fact,
  once derived, carries no memory of the syntax that declared it.
