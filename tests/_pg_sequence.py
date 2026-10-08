"""The statement every door runs on Postgres after a table or serial key
rename, written out once for the tests that expect it.

```python
class Author(Model):
    __ferro_renamed_from__ = "writer"
```

``ALTER TABLE "writer" RENAME TO "author"`` keeps the key's sequence named
``writer_id_seq``, where a table created as ``author`` owns
``author_id_seq``; this statement carries it to that name, and refuses, naming
the holder and the fix, when another relation already holds it
(``ferro_ddl_lowering::render_pg_serial_sequence_rename``).
"""


def pg_sequence_rename(table: str, column: str = "id") -> str:
    """The sequence rename for ``table``'s serial ``column``, both plain
    identifiers short enough that Postgres does not cut the name."""
    target = f"{table}_{column}_seq"
    return (
        f"DO $$ DECLARE seq regclass := pg_get_serial_sequence('\"{table}\"', "
        f"'{column}')::regclass; holder regclass; kind text; BEGIN IF seq IS NOT NULL "
        f"AND (SELECT relname FROM pg_class WHERE oid = seq) <> '{target}' THEN SELECT "
        "c.oid, CASE c.relkind WHEN 'S' THEN 'sequence' WHEN 'r' THEN 'table' WHEN 'p' "
        "THEN 'table' WHEN 'i' THEN 'index' WHEN 'I' THEN 'index' WHEN 'v' THEN 'view' "
        "WHEN 'm' THEN 'materialized view' ELSE 'relation' END INTO holder, kind FROM "
        f"pg_class c WHERE c.relname = '{target}' AND c.relnamespace = (SELECT "
        "relnamespace FROM pg_class WHERE oid = seq); IF holder IS NOT NULL THEN RAISE "
        "EXCEPTION USING ERRCODE = 'duplicate_table', MESSAGE = format('Cannot rename "
        "sequence %s, owned by %s, to %I, the name a table created as %s gives it: %s "
        "%s already holds that name. Rename or drop that %s, then run the change "
        f"again.', seq, '\"{table}\".\"{column}\"', '{target}', '\"{table}\"', kind, "
        "holder, kind); END IF; EXECUTE format('ALTER SEQUENCE %s RENAME TO %I', seq, "
        f"'{target}'); END IF; END $$"
    )
