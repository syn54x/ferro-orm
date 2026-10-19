# Renaming and retiring columns and labels

This page shows how to rename a table, a column or an enum label without losing the rows it holds, and how to retire a column or a label over two releases so the release still serving keeps working. Every section starts from this `Author`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_rename.py:base"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_rename_annotated.py:base"
    ```

Its first migration, `0001_create_author`, is applied, and each section below changes these models once. The `# before:` and `# new` comments in each section mark the change against them; they are not part of the declaration.

## Rename hints

Without a hint, a renamed column is a dropped column and an added one, and `new` writes exactly that, marked destructive, and says what it saw. Rename `nickname` to `handle` with no hint, and `ferro migrate new rename_handle` ends with the line that names the fix:

<!-- output: rename-hint -->
```text
default · migrations
ok Wrote migrations/0002_rename_handle/
  01_schema.up.postgres.sql               schema step, up (postgres)
  01_schema.down.postgres.sql             schema step, down (postgres)
  01_schema.up.sqlite.sql                 schema step, up (sqlite)
  01_schema.down.sqlite.sql               schema step, down (sqlite)
  02_idx_author_handle.up.postgres.sql    schema step, up (postgres)
  02_idx_author_handle.down.postgres.sql  schema step, down (postgres)
  02_idx_author_handle.up.sqlite.sql      schema step, up (sqlite)
  02_idx_author_handle.down.sqlite.sql    schema step, down (sqlite)
  ir.json                                 snapshot

  1 changed model
    Author
  - author: if "nickname" became "handle", declare renamed_from="nickname"
```

Every author's nickname would be lost. With a hint, `new` writes a rename instead, and every row keeps its value. A hint says what a table, a column or an enum label was called before; here all three are renamed at once:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_rename.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_rename_annotated.py:models"
    ```

`ferro migrate new rename_writer` writes the renames, and nothing is dropped:

```sql
-- 0002_rename_writer/01_schema.up.postgres.sql
ALTER TABLE "author" RENAME TO "writer";

DO $$ DECLARE seq regclass := pg_get_serial_sequence('"writer"', 'id')::regclass; holder regclass; kind text; BEGIN IF seq IS NOT NULL AND (SELECT relname FROM pg_class WHERE oid = seq) <> 'writer_id_seq' THEN SELECT c.oid, CASE c.relkind WHEN 'S' THEN 'sequence' WHEN 'r' THEN 'table' WHEN 'p' THEN 'table' WHEN 'i' THEN 'index' WHEN 'I' THEN 'index' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' ELSE 'relation' END INTO holder, kind FROM pg_class c WHERE c.relname = 'writer_id_seq' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = seq); IF holder IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE = 'duplicate_table', MESSAGE = format('Cannot rename sequence %s, owned by %s, to %I, the name a table created as %s gives it: %s %s already holds that name. Rename or drop that %s, then run the change again.', seq, '"writer"."id"', 'writer_id_seq', '"writer"', kind, holder, kind); END IF; EXECUTE format('ALTER SEQUENCE %s RENAME TO %I', seq, 'writer_id_seq'); END IF; END $$;

ALTER TABLE "writer" RENAME COLUMN "nickname" TO "handle";

ALTER INDEX "idx_author_bio" RENAME TO "idx_writer_bio";

ALTER INDEX "idx_author_nickname" RENAME TO "idx_writer_handle";

ALTER INDEX "uq_author_email" RENAME TO "uq_writer_email";

ALTER TYPE "status" RENAME VALUE 'canceled' TO 'cancelled';
```

The table, the column, every index named after them and the label are renamed in place. The hints:

- `Field(renamed_from="nickname")` renames a column; on a relation, `ForeignKey(..., renamed_from="writer")` renames its `*_id` column.
- `__ferro_renamed_from__ = "author"` on a model renames its table, and every index and constraint named after it. On Postgres the `DO` block renames the table's `serial` sequence too, to `writer_id_seq`, the name a table created as `writer` gives it; it refuses, naming the object, if something else already holds that name.
- `__ferro_renamed_labels__ = {"cancelled": "canceled"}` on a `StrEnum` maps each new label to its old one. On SQLite, where labels are text in the rows, the rows are rewritten in a [table rebuild](migrations.md#sqlite-table-rebuilds).
- An enum *type*'s rename needs no hint: it is read off the columns that moved to it.

A hint is live only while the previous snapshot still holds the old name and lacks the new one. Once its migration is generated it is inert and may be deleted. A hint whose old name is still declared, or two hints naming one old name, is refused.

Auto-migrate honours every hint under `migrate_updates` (on SQLite a label hint only warns, since the labels live in the rows). Without `migrate_updates`, a table hint makes the create pass hold the table back with a warning rather than create it empty. The [door coverage table](../../reference/migrations.md#what-each-door-covers) lists what each door does with a hint.

The example `docs/examples/migrations_rename.py` in the repository runs this rename end to end, the revert included.

## Renaming under a rolling deploy

A rename hint renames in one migration, so the previous release's code, still naming `nickname`, fails the moment the migration runs. When old and new code serve side by side, rename over three releases instead:

1. Add `handle` beside `nickname`, write both in the application code, and copy the existing rows across in a [data step](data-steps.md#a-data-step).
2. [Retire](#retiring-a-column) `nickname`.
3. Delete its declaration, which drops it.

Each release is a migration the release before it can run beside; the [step classes](../../reference/migrations.md#step-classes) say which steps that holds for.

## Retiring a column

Dropping a column in the release that stops using it breaks the release still serving: its code names the column in every `INSERT` and `UPDATE`, so it fails from the moment the drop runs until it is replaced. A column leaves in two releases instead. In the first, declare it `retired`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_retire.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_retire_annotated.py:models"
    ```

From this release on, `bio` is gone from the model:

- `author.bio` raises `AttributeError`, and `Author(bio="x")` raises `TypeError`.
- A query, `order_by`, `update()` or `save(only=...)` naming it fails with "retired in this release".
- No `SELECT`, `INSERT` or `UPDATE` the model issues names it.

The column stays in the schema snapshot, nullable, with its index, so `ferro migrate new retire_bio` writes only the relaxation the previous release needs to keep writing beside this one:

```sql
-- 0002_retire_bio/01_schema.up.postgres.sql
ALTER TABLE "author" ALTER COLUMN "bio" DROP NOT NULL;
```

The previous release can still insert an author with a `bio`, and this one inserts authors without it. On SQLite the same change is a [table rebuild](migrations.md#sqlite-table-rebuilds). Retiring a column that already accepts `NULL` writes nothing: `new` says "no schema change" and `check` is clean.

In the next release, delete the declaration: `new` writes the `DROP COLUMN`, marked destructive, and, when it was the last column of a Postgres enum type, the `DROP TYPE` after it.

| Release | `bio` in the model | The migration | Old code beside it |
| :--- | :--- | :--- | :--- |
| A | required, indexed | creates `bio` | |
| B | nullable, indexed, `retired=True` | `DROP NOT NULL` | A writes `bio`; works |
| C | deleted | `DROP COLUMN "bio"` (destructive) | B never names `bio`; works |

- `ForeignKey(retired=True)` retires a relation and its `*_id` column; its foreign key constraint stays until the column is dropped. It needs no `related_name`, and a `BackRef` reading it back is refused: delete it.
- Nothing else about the column changes while it is retired: its index, unique, check and foreign key, and every table check, row policy, composite index or unique that names it, stay as declared.
- A [historical model](data-steps.md#historical-models) sees a retired column as an ordinary nullable column, so a data step can still read it.
- Auto-migrate creates the column on a fresh database and, under `migrate_updates`, relaxes its `NOT NULL`; it never drops it. Alembic autogenerate writes the same `alter_column(..., nullable=True)`.
- Refused at class definition, each naming the fix: retiring a field whose annotation does not allow `None` (declare it `str | None`), a field declared `nullable=False` (a retired column stays nullable), a primary key (drop `retired=True` and change the key first, see [Changing a primary key](migrations.md#changing-a-primary-key)), a field that also declares `renamed_from` (retire it or rename it, not both), a `BackRef` or a `ManyToMany`.

The example `docs/examples/migrations_retire.py` runs all three releases end to end.

## Retiring an enum label

Removing a label in the release that stops writing it breaks the release still serving: its code may still write the label, and a removed label is gone from the Postgres type and from any `db_check`. Stop writing the label first, and remove it a release later. In the first release, declare it retired on the `StrEnum`, beside the [`__ferro_renamed_labels__`](#rename-hints) hint:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_retire_label.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_retire_label_annotated.py:models"
    ```

The member stays, so a row that holds `paused` still loads as `Status.PAUSED`. Writing the label is refused, at construction and by attribute assignment. `Author(name="Grace Hopper", email="grace@example.com", bio="Admiral", status="paused")` raises a validation error:

```text
1 validation error for Author
status
  Status.PAUSED ('paused') is retired in this release: the previous release may still write it, this one does not [type=retired_enum_label, input_value='paused', input_type=str]
```

Nothing was saved. A row loaded with the label can still be saved after another field changes: loading does not validate, and `save()` writes every column, so the unchanged label goes back as it was. That is not a write of the label.

Retiring a label is not a schema change. The Postgres type and any `db_check` keep listing it, so `ferro migrate new retire_paused` says "no schema change", writes nothing, and `check` is clean. In the next release, delete the member and the declaration together: `new` then writes the ordinary [label removal](data-steps.md#removing-an-enum-label), the backfill for rows that still hold it, and on Postgres the type swap.

Each label in `__ferro_retired_labels__` must be a member's value: a set of anything else, or a label that names no member, raises `TypeError` at class definition. The example `docs/examples/migrations_retire_label.py` runs both releases.

## See also

- [Migrations](migrations.md): the loop these changes run through
- [Data steps and backfills](data-steps.md): the copy step of an online rename, and a label's removal
- [Deploying migrations](deploying.md#rolling-deploys-and-the-two-release-contract): the two-release contract
- [Migrations reference](../../reference/migrations.md): step classes and what each door covers
