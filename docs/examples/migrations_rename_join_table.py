# ruff: noqa: E402 - the models come first, as the docs page shows them
"""Runnable companion for renaming a join table
(docs/pages/guide/schema/renames-and-retirements.md).

The page's `Author`, tagged through a join table named `writer_tags`, renames
that table to `author_tags` in one migration, applied over two link rows and
reverted, on SQLite.
"""

# --8<-- [start:models]
from enum import StrEnum

from ferro import Field, ManyToMany, Model, Relation


class Status(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELED = "canceled"


class Tag(Model):
    id: int | None = Field(default=None, primary_key=True)
    label: str = Field(unique=True)


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    nickname: str | None = Field(default=None, index=True)
    bio: str = Field(index=True)
    status: Status = Status.ACTIVE
    tags: Relation[list[Tag]] = ManyToMany(
        through="author_tags",  # before: through="writer_tags"
        renamed_from="writer_tags",  # new
    )


# --8<-- [end:models]


from _migrations_project import project


def main() -> None:
    with project(__file__) as app:
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.ferro("migrate", "up")
        app.sql(
            "INSERT INTO author (name, email, bio, status) VALUES"
            " ('Ada Lovelace', 'ada@example.com', 'Analyst', 'active')"
        )
        app.sql("INSERT INTO tag (label) VALUES ('mathematics'), ('poetry')")
        app.sql("INSERT INTO writer_tags (author_id, tag_id) VALUES (1, 1), (1, 2)")

        app.models_after()  # through="author_tags", renamed_from="writer_tags"
        app.ferro("migrate", "new", "rename_author_tags")
        up = (
            app.migrations / "0002_rename_author_tags" / "01_schema.up.postgres.sql"
        ).read_text()
        assert 'ALTER TABLE "writer_tags" RENAME TO "author_tags";' in up
        assert "CREATE TABLE" not in up and "DROP TABLE" not in up

        app.ferro("migrate", "up")
        assert "no drift" in app.ferro("migrate", "drift")
        links = "SELECT author_id, tag_id FROM {} ORDER BY tag_id"
        assert app.sql(links.format("author_tags")) == [(1, 1), (1, 2)]

        app.ferro("migrate", "down", "--yes")
        assert app.sql(links.format("writer_tags")) == [(1, 1), (1, 2)]
        app.ferro("migrate", "up")


if __name__ == "__main__":
    main()
