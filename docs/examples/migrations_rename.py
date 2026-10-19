# ruff: noqa: E402, F811 - the models come first, as the docs page shows them: before, then after
"""Runnable companion for rename hints
(docs/pages/guide/schema/renames-and-retirements.md).

A table, a column and an enum label renamed in one migration, applied over a
row and reverted, on SQLite.
"""

# --8<-- [start:base]
from enum import StrEnum

from ferro import Field, Model


class Status(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELED = "canceled"


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    nickname: str | None = Field(default=None, index=True)
    bio: str = Field(index=True)
    status: Status = Status.ACTIVE


# --8<-- [end:base]

# --8<-- [start:models]
from enum import StrEnum

from ferro import Field, Model


class Status(StrEnum):
    __ferro_renamed_labels__ = {"cancelled": "canceled"}  # new
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"  # before: CANCELED = "canceled"


class Writer(Model):  # before: class Author(Model):
    __ferro_renamed_from__ = "author"  # new
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    # before: nickname: str | None = Field(default=None, index=True)
    handle: str | None = Field(default=None, index=True, renamed_from="nickname")
    bio: str = Field(index=True)
    status: Status = Status.ACTIVE


# --8<-- [end:models]


from _migrations_project import project


def main() -> None:
    with project(__file__) as app:
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.ferro("migrate", "up")
        app.sql(
            "INSERT INTO author (name, email, nickname, bio, status) VALUES"
            " ('Ada Lovelace', 'ada@example.com', 'ada', 'Analyst', 'canceled')"
        )

        app.models_after()  # the three rename hints
        written = app.ferro("migrate", "new", "rename_writer")
        assert "  1 renamed enum label\n    status.canceled → cancelled\n" in written
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
