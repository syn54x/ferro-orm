# ruff: noqa: E402 - the models come first, as the docs page shows them
"""Runnable companion for retiring an enum label
(docs/pages/guide/schema/renames-and-retirements.md).

Release A writes ``paused``. Release B retires it: the member stays so rows
holding it still load, the label stays in the database, and the migration
says "no schema change". On SQLite.
"""

# --8<-- [start:models]
from enum import StrEnum
from typing import Annotated

from ferro import Field, Model


class Status(StrEnum):
    __ferro_retired_labels__ = frozenset({"paused"})  # new
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELED = "canceled"


class Author(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: str
    email: Annotated[str, Field(unique=True)]
    nickname: Annotated[str | None, Field(index=True)] = None
    bio: Annotated[str, Field(index=True)]
    status: Status = Status.ACTIVE


# --8<-- [end:models]


import subprocess
import sys

from _migrations_project import project

PROBE = """
from blog.models import Author, Status

# A row release A wrote hydrates as the member and can change another field.
author = Author.model_construct(id=1, status=Status.PAUSED)
author.bio = "Mathematician"
assert author.status is Status.PAUSED

# This release does not write the label.
try:
    Author(
        name="Grace Hopper", email="grace@example.com", bio="Admiral", status="paused"
    )
except ValueError as error:
    assert "Status.PAUSED ('paused') is retired in this release" in str(error)
else:
    raise SystemExit("the retired label was accepted")
"""


def main() -> None:
    with project(__file__) as app:
        # Release A creates the table; its code writes "paused".
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.ferro("migrate", "up")
        app.sql(
            "INSERT INTO author (name, email, bio, status) VALUES"
            " ('Ada Lovelace', 'ada@example.com', 'Analyst', 'paused')"
        )

        # Release B retires the label: nothing to migrate, nothing to check.
        app.models_after()
        assert "no schema change" in app.ferro("migrate", "new", "retire_paused")
        assert not (app.migrations / "0002_retire_paused").exists()
        app.ferro("migrate", "check")

        # --8<-- [start:retired-write]
        try:
            Author(
                name="Grace Hopper",
                email="grace@example.com",
                bio="Admiral",
                status="paused",
            )
        except ValueError as error:
            print(error)
        # --8<-- [end:retired-write]

        subprocess.run(
            [sys.executable, "-c", PROBE], cwd=app.root, check=True, timeout=120
        )
        assert app.sql("SELECT status FROM author") == [("paused",)]


if __name__ == "__main__":
    main()
