# ruff: noqa: E402 - the models come first, as the docs page shows them
"""Runnable companion for rename hints, Annotated style
(docs/pages/guide/schema/migrations.md).

A table, a column and an enum label renamed in one migration, applied over a
row and reverted, on SQLite.
"""

# --8<-- [start:models]
from enum import StrEnum
from typing import Annotated

from ferro import FerroField, Model


class Status(StrEnum):
    __ferro_renamed_labels__ = {"cancelled": "canceled"}  # new
    DRAFT = "draft"
    CANCELLED = "cancelled"  # before: CANCELED = "canceled"


class Writer(Model):  # before: class Author(Model):
    __ferro_renamed_from__ = "author"  # new
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    # before: nickname: Annotated[str, FerroField(index=True)]
    handle: Annotated[str, FerroField(index=True, renamed_from="nickname")]
    status: Status = Status.DRAFT


# --8<-- [end:models]


from _migrations_project import project


def main() -> None:
    with project(__file__) as app:
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.ferro("migrate", "up")
        app.sql("INSERT INTO author (nickname, status) VALUES ('ada', 'canceled')")

        app.models_after()  # the three rename hints
        written = app.ferro("migrate", "new", "rename_writer")
        assert "renamed enum labels: status.canceled → cancelled" in written
        up = (
            app.migrations / "0002_rename_writer" / "01_schema.up.postgres.sql"
        ).read_text()
        assert 'ALTER TABLE "author" RENAME TO "writer";' in up
        assert 'ALTER TABLE "writer" RENAME COLUMN "nickname" TO "handle";' in up
        assert "ALTER TYPE \"status\" RENAME VALUE 'canceled' TO 'cancelled';" in up

        app.ferro("migrate", "up")
        assert "no drift" in app.ferro("migrate", "drift")
        assert app.sql("SELECT handle, status FROM writer") == [("ada", "cancelled")]

        app.ferro("migrate", "down", "--yes")
        assert "no drift against 0001_create_author" in app.ferro("migrate", "drift")
        assert app.sql("SELECT nickname, status FROM author") == [("ada", "canceled")]
        app.ferro("migrate", "up")


if __name__ == "__main__":
    main()
