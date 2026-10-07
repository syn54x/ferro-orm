# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate new`` for a declared rename (#528, ADR-0032).

```python
class Author(Model):
    __ferro_renamed_from__ = "writer"                 # table rename
    full_name: str = Field(renamed_from="name")       # column rename
```

```text
0002_rename_writer/
  01_schema.up.postgres.sql   ALTER TABLE "writer" RENAME TO "author";
                              ALTER TABLE "author" RENAME COLUMN "name" TO "full_name";
                              ALTER INDEX "idx_writer_name" RENAME TO "idx_author_full_name";
                              ALTER TABLE "book" RENAME CONSTRAINT "fk_book_writer_id_writer" TO "fk_book_writer_id_author";
  01_schema.up.sqlite.sql     ALTER TABLE "writer" RENAME TO "author"; … RENAME COLUMN …;
                              DROP INDEX "idx_writer_name"; CREATE INDEX "idx_author_full_name" …;
                              -- plus the rebuild of every table whose ck_/fk_ name moved
```

Every round trip builds a real project under ``tmp_path``, applies the parent
migration against the parametrized database (SQLite and Postgres), generates
the rename, applies it on a populated database, and checks there is no drift
against the new snapshot, that ``down`` reverts it with no drift against the
parent, and that every row survives both ways.
"""

from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation


def build_rename_hint_models() -> None:
    """Every rename hint, in both declaration styles (ADR-0032). Pins the
    ``schema_rename_hints_v2`` golden vector."""

    class Publisher(Model):
        id: int | None = Field(default=None, primary_key=True)
        books: Relation[list["Book"]] = BackRef()

    class Book(Model):
        __ferro_renamed_from__ = "volume"
        id: int | None = Field(default=None, primary_key=True)
        full_name: str = Field(index=True, renamed_from="name")
        subtitle: Annotated[str, Field(renamed_from="tagline")]
        house: Annotated[Publisher, ForeignKey("books", renamed_from="press")]

    _ = (Publisher, Book)
