# ruff: noqa: E402, F811 - the models come first, as the docs page shows them, once per task
"""Runnable companion for squashing (docs/pages/guide/schema/squash.md).

Two tasks on one branch: the first renames ``author.nickname`` to ``handle``
with a rename hint, the second deletes the hint, now inert, and adds ``Team``.
Squashed, the two migrations are one with one ``RENAME COLUMN``; a database
that applied both inherits it and runs nothing; on SQLite.
"""

# --8<-- [start:models]
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    # before: nickname: str | None = Field(default=None, index=True)
    handle: str | None = Field(default=None, index=True, renamed_from="nickname")


# --8<-- [end:models]

# --8<-- [start:later]
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    handle: str | None = Field(default=None, index=True)


class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    slug: str


# --8<-- [end:later]


from _migrations_project import project


def main() -> None:
    with project(__file__) as app:
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.ferro("migrate", "up")
        app.sql(
            "INSERT INTO author (name, email, nickname) VALUES"
            " ('Ada Lovelace', 'ada@example.com', 'ada')"
        )

        app.models_after()  # task one: the rename hint
        app.ferro("migrate", "new", "rename_handle")
        app.ferro("migrate", "up")

        app.models_region("later")  # task two: the hint deleted, Team added
        app.ferro("migrate", "new", "add_teams")
        app.ferro("migrate", "up")

        written = app.ferro("migrate", "squash", "teams", "--from", "0002")
        assert "replaces 0002_rename_handle … 0003_add_teams, now deleted" in written
        assert sorted(path.name for path in app.migrations.glob("0*")) == [
            "0001_create_author",
            "0002_teams",
        ]
        up = (app.migrations / "0002_teams" / "01_schema.up.postgres.sql").read_text()
        assert 'ALTER TABLE "author" RENAME COLUMN "nickname" TO "handle";' in up
        assert 'CREATE TABLE IF NOT EXISTS "team"' in up
        assert "DROP COLUMN" not in up

        # This database applied 0002_rename_handle and 0003_add_teams: it inherits.
        status = app.ferro("migrate", "status", exit_code=3)
        assert "0002_teams          pending (inheritable)" in status
        inherited = app.ferro("migrate", "up")
        assert (
            "inherited 0002_teams: this database had applied "
            "0002_rename_handle … 0003_add_teams; nothing was run"
        ) in inherited
        assert "applied (inherited)" in app.ferro("migrate", "status")
        assert "no drift against 0002_teams" in app.ferro("migrate", "drift")
        assert app.sql("SELECT handle FROM author") == [("ada",)]

        # An inherited migration's down runs: the rename is reverted.
        app.ferro("migrate", "down", "--yes")
        assert "no drift against 0001_create_author" in app.ferro("migrate", "drift")
        assert app.sql("SELECT nickname FROM author") == [("ada",)]

        # Run for real, the squash renames the column and keeps the row.
        app.ferro("migrate", "up")
        assert "no drift against 0002_teams" in app.ferro("migrate", "drift")
        assert app.sql("SELECT handle FROM author") == [("ada",)]
        assert app.sql("SELECT count(*) FROM team") == [(0,)]


if __name__ == "__main__":
    main()
