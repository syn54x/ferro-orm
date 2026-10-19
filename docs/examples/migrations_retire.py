# ruff: noqa: E402 - the models come first, as the docs page shows them
"""Runnable companion for retiring a column
(docs/pages/guide/schema/renames-and-retirements.md).

Release A declares ``bio`` required. Release B retires it: the migration only
relaxes ``NOT NULL``, so A's code keeps writing ``bio`` while B's never names
it. Release C deletes the declaration, which drops the column. On SQLite.
"""

# --8<-- [start:models]
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
    # before: bio: str = Field(index=True)
    bio: str | None = Field(default=None, index=True, retired=True)
    status: Status = Status.ACTIVE


# --8<-- [end:models]


from _migrations_project import project


def main() -> None:
    with project(__file__) as app:
        # Release A creates the table; its code writes bio.
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.ferro("migrate", "up")
        app.sql(
            "INSERT INTO author (name, email, bio, status) VALUES"
            " ('Ada Lovelace', 'ada@example.com', 'Analyst', 'active')"
        )

        # Release B retires bio: one change, the relaxation.
        app.models_after()
        app.ferro("migrate", "new", "retire_bio")
        up = (
            app.migrations / "0002_retire_bio" / "01_schema.up.postgres.sql"
        ).read_text()
        assert up == 'ALTER TABLE "author" ALTER COLUMN "bio" DROP NOT NULL;\n'
        app.ferro("migrate", "up")
        assert "no drift" in app.ferro("migrate", "drift")
        # Release A's write (names bio) and B's (does not) both succeed.
        app.sql(
            "INSERT INTO author (name, email, bio, status) VALUES"
            " ('Grace Hopper', 'grace@example.com', 'Admiral', 'active')"
        )
        app.sql(
            "INSERT INTO author (name, email, status) VALUES"
            " ('Linus Torvalds', 'linus@example.com', 'active')"
        )

        # Release C deletes the declaration: the destructive drop.
        (app.root / "blog" / "models.py").write_text(
            "".join(
                line
                for line in app.region.splitlines(keepends=True)
                if "bio" not in line
            )
        )
        app.ferro("migrate", "new", "drop_bio")
        drop = (
            app.migrations / "0003_drop_bio" / "01_schema.up.sqlite.sql"
        ).read_text()
        assert drop.startswith("-- ferro: destructive\n")
        assert 'ALTER TABLE "author" DROP COLUMN "bio";' in drop
        app.ferro("migrate", "up")
        assert "no drift" in app.ferro("migrate", "drift")
        assert app.sql("SELECT name FROM author ORDER BY id") == [
            ("Ada Lovelace",),
            ("Grace Hopper",),
            ("Linus Torvalds",),
        ]


if __name__ == "__main__":
    main()
