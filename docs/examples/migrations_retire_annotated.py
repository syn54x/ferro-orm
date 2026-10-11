# ruff: noqa: E402 - the models come first, as the docs page shows them
"""Runnable companion for retiring a column, Annotated style
(docs/pages/guide/schema/migrations.md).

Release A declares ``bio`` required. Release B retires it: the migration only
relaxes ``NOT NULL``, so A's code keeps writing ``bio`` while B's never names
it. Release C deletes the declaration, which drops the column. On SQLite.
"""

# --8<-- [start:models]
from typing import Annotated

from ferro import FerroField, Model


class Author(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    # before: bio: Annotated[str, FerroField(index=True)]
    bio: Annotated[str | None, FerroField(index=True, retired=True)] = None


# --8<-- [end:models]


from _migrations_project import project


def main() -> None:
    with project(__file__) as app:
        # Release A creates the table; its code writes bio.
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.ferro("migrate", "up")
        app.sql("INSERT INTO author (name, bio) VALUES ('ada', 'pioneer')")

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
        app.sql("INSERT INTO author (name, bio) VALUES ('grace', 'admiral')")
        app.sql("INSERT INTO author (name) VALUES ('linus')")

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
            ("ada",),
            ("grace",),
            ("linus",),
        ]


if __name__ == "__main__":
    main()
