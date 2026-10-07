# ruff: noqa: E402 - the models come first, as the docs page shows them
"""Runnable companion for backfills and guards (docs/pages/guide/schema/data-steps.md).

A required column added to a table that has rows: the guard refuses, the
backfill is written where it says todo(...), the late-row recovery is
``down --to 0002:01`` then ``up``, all on SQLite.
"""

# --8<-- [start:models]
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    slug: str  # new


# --8<-- [end:models]


from _migrations_project import project

TODO = '        author.slug = todo("the slug for an existing author")\n'
WRITTEN = '        author.slug = author.name.lower().replace(" ", "-")\n'


def main() -> None:
    with project(__file__) as app:
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.ferro("migrate", "up")
        app.sql("INSERT INTO author (name) VALUES ('Ada Lovelace'), ('Grace Hopper')")

        app.models_after()  # Author gains a required slug

        # The guard: "no existing author needs a slug". These two do, so it fails.
        written = app.ferro(
            "migrate", "new", "author_slug", "--no-backfill", "author.slug"
        )
        assert "02_guard_author.py" in written
        refused = app.ferro("migrate", "up", exit_code=1)
        assert "2 author rows still have NULL in slug" in refused
        app.ferro("migrate", "down", "--yes")  # its expand step was applied
        guarded = app.migrations / "0002_author_slug"
        for path in sorted(guarded.iterdir()):
            path.unlink()
        guarded.rmdir()

        # The backfill: refused until the todo is written.
        written = app.ferro("migrate", "new", "author_slug")
        assert "02_backfill_author.py needs writing" in written
        backfill = app.migrations / "0002_author_slug" / "02_backfill_author.py"
        assert TODO in backfill.read_text()
        assert "not written yet" in app.ferro("migrate", "up", exit_code=1)
        backfill.write_text(backfill.read_text().replace(TODO, WRITTEN))
        app.ferro("migrate", "up")
        assert app.ferro("migrate", "check").startswith("ok: models match")
        assert "no drift" in app.ferro("migrate", "drift")
        assert app.sql("SELECT slug FROM author ORDER BY id") == [
            ("ada-lovelace",),
            ("grace-hopper",),
        ]

        # Back to just after the expand step, and forward again.
        app.ferro("migrate", "down", "--to", "0002:01", "--yes")
        assert "02_backfill_author.py            pending" in app.ferro(
            "migrate", "status", "--steps", exit_code=3
        )
        app.ferro("migrate", "up")
        app.ferro("migrate", "down", "--to", "0001", "--yes")
        assert "no drift against 0001_create_author" in app.ferro("migrate", "drift")


if __name__ == "__main__":
    main()
