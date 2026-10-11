# ruff: noqa: E402 - the models come first, as the docs page shows them
"""Runnable companion for retiring an enum label
(docs/pages/guide/schema/migrations.md).

Release A writes ``canceled``. Release B retires it: the member stays so rows
holding it still load, the label stays in the database, and the migration
says "no schema change". On SQLite.
"""

# --8<-- [start:models]
from enum import StrEnum

from ferro import Field, Model


class Status(StrEnum):
    __ferro_retired_labels__ = frozenset({"canceled"})  # new
    PAID = "paid"
    CANCELED = "canceled"


class Order(Model):
    id: int | None = Field(default=None, primary_key=True)
    status: Status = Status.PAID
    note: str = ""


# --8<-- [end:models]


import subprocess
import sys

from _migrations_project import project

PROBE = """
from blog.models import Order, Status

# A row release A wrote hydrates as the member and can change another field.
order = Order.model_construct(id=1, status=Status.CANCELED)
order.note = "b"
assert order.status is Status.CANCELED

# This release does not write the label.
try:
    Order(status="canceled")
except ValueError as error:
    assert "Status.CANCELED ('canceled') is retired in this release" in str(error)
else:
    raise SystemExit("the retired label was accepted")
"""


def main() -> None:
    with project(__file__) as app:
        # Release A creates the table; its code writes "canceled".
        app.models_before()
        app.ferro("migrate", "new", "create_order")
        app.ferro("migrate", "up")
        app.sql("INSERT INTO \"order\" (status, note) VALUES ('canceled', 'a')")

        # Release B retires the label: nothing to migrate, nothing to check.
        app.models_after()
        assert "no schema change" in app.ferro("migrate", "new", "retire_canceled")
        assert not (app.migrations / "0002_retire_canceled").exists()
        app.ferro("migrate", "check")

        subprocess.run(
            [sys.executable, "-c", PROBE], cwd=app.root, check=True, timeout=120
        )
        assert app.sql('SELECT status FROM "order"') == [("canceled",)]


if __name__ == "__main__":
    main()
