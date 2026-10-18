# ruff: noqa: E402, F811 - the models come first, as the docs page shows them, once per task
"""Runnable companion for squashing (docs/pages/guide/schema/migrations.md).

Two tasks on one branch: the first renames ``author.nickname`` to ``handle``
with a rename hint, the second deletes the hint, now inert, and adds ``bio``.
Squashed, the two migrations are one with one ``RENAME COLUMN``; a database
that applied both adopts it and runs nothing; on SQLite.
"""

# --8<-- [start:models]
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    # before: nickname: str | None = None
    handle: str | None = Field(default=None, renamed_from="nickname")


# --8<-- [end:models]

# --8<-- [start:later]
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    handle: str | None = None
    bio: str | None = None


# --8<-- [end:later]


from _migrations_project import project


def main() -> None:
    with project(__file__) as app:
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.ferro("migrate", "up")
        app.sql("INSERT INTO author (nickname) VALUES ('ada')")

        app.models_after()  # task one: the rename hint
        app.ferro("migrate", "new", "rename_handle")
        app.ferro("migrate", "up")

        app.models_region("later")  # task two: the hint deleted, bio added
        app.ferro("migrate", "new", "add_bio")
        app.ferro("migrate", "up")

        written = app.ferro("migrate", "squash", "author_profile", "--from", "0002")
        assert "replaces 0002_rename_handle … 0003_add_bio, now deleted" in written
        assert sorted(path.name for path in app.migrations.glob("0*")) == [
            "0001_create_author",
            "0002_author_profile",
        ]
        up = (
            app.migrations / "0002_author_profile" / "01_schema.up.postgres.sql"
        ).read_text()
        assert 'ALTER TABLE "author" RENAME COLUMN "nickname" TO "handle";' in up
        assert 'ADD COLUMN "bio"' in up
        assert "DROP COLUMN" not in up

        # This database applied 0002_rename_handle and 0003_add_bio: it adopts.
        status = app.ferro("migrate", "status", exit_code=3)
        assert "0002_author_profile  pending (adoptable)" in status
        adopted = app.ferro("migrate", "up")
        assert (
            "adopted 0002_author_profile: this database had applied "
            "0002_rename_handle … 0003_add_bio; nothing was run"
        ) in adopted
        assert "applied (adopted)" in app.ferro("migrate", "status")
        assert "no drift against 0002_author_profile" in app.ferro("migrate", "drift")
        assert app.sql("SELECT handle FROM author") == [("ada",)]

        # An adopted migration's down runs: the rename is reverted.
        app.ferro("migrate", "down", "--yes")
        assert "no drift against 0001_create_author" in app.ferro("migrate", "drift")
        assert app.sql("SELECT nickname FROM author") == [("ada",)]

        # Run for real, the squash renames the column and keeps the row.
        app.ferro("migrate", "up")
        assert "no drift against 0002_author_profile" in app.ferro("migrate", "drift")
        assert app.sql("SELECT handle, bio FROM author") == [("ada", None)]


if __name__ == "__main__":
    main()
