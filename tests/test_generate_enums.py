# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate new`` for an edit to a ``StrEnum`` (#529; ADR-0011, ADR-0032).

```python
class EnmOrderStatus(StrEnum):
    __ferro_renamed_labels__ = {"cancelled": "canceled"}   # new label → old label
    PAID = "paid"
    CANCELLED = "cancelled"
```

```text
0002_status_labels/                       # REFUNDED = "refunded" added
  01_labels.up.postgres.sql     ALTER TYPE "enmorderstatus" ADD VALUE IF NOT EXISTS 'refunded';
  01_labels.down.postgres.sql   -- ferro: nothing-to-reverse Postgres cannot drop an enum label; 'refunded' stays
  01_labels.up.sqlite.sql       -- ferro: not-applicable
0002_rename_label/                        # the hint above
  01_schema.up.postgres.sql     ALTER TYPE "enmorderstatus" RENAME VALUE 'canceled' TO 'cancelled';
  01_schema.up.sqlite.sql       UPDATE "enmorder" SET "status" = 'cancelled' WHERE "status" = 'canceled';
0002_rename_type/                         # class renamed to EnmOrderState, no hint
  01_schema.up.postgres.sql     ALTER TYPE "enmorderstatus" RENAME TO "enmorderstate";
```

Every project targets both dialects, so each migration carries a Postgres and
a SQLite rendering; each test applies it to the parametrized database, checks
there is no drift, reverts it, and checks there is no drift against the
parent. Table and type names start with ``enm`` so this module never shares a
name with another suite on the same Postgres server.
"""

import contextlib
import io
import json
from pathlib import Path

import pytest

from ferro import _core
from tests.test_generate_columns import (  # noqa: F401 - fixtures
    no_bytecode,
    refused,
    round_trip,
)
from tests.test_migrate_down import (  # noqa: F401 - fixtures
    keys,
    migration_dir,
    plan_against,
    snapshot,
)
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    isolated_imports,
    pkg,
    project,
    run,
    statements,
    write_config,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    db,
    new,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode"
)

backend_matrix = pytest.mark.backend_matrix

BOTH = ("postgres", "sqlite")


def models(
    labels: tuple[str, ...] = ("paid", "canceled"),
    hint: str = "",
    cls: str = "EnmOrderStatus",
    refund: bool = True,
    extra: str = "",
) -> str:
    """``EnmOrder`` (and ``EnmRefund``) with a ``status`` of the enum ``cls``."""
    members = "".join(f'    {label.upper()} = "{label}"\n' for label in labels)
    renamed = f"    __ferro_renamed_labels__ = {hint}\n" if hint else ""
    refund_model = (
        f"""

class EnmRefund(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    status: {cls}
"""
        if refund
        else ""
    )
    return (
        f"""
class {cls}(StrEnum):
{renamed}{members}

class EnmOrder(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    status: {cls}
{extra}"""
        + refund_model
    )


def start(project: Path, pkg: str, db, body: str) -> None:
    """``0001`` creates ``body``'s models for both dialects and is applied."""
    write_config(project, pkg)
    write_models(project, pkg, body)
    new("create")
    assert run("migrate", "up", "--url", db.url) == 0


def step_files(project: Path, number: int) -> list[str]:
    return sorted(p.name for p in migration_dir(project, number).iterdir())


def step_file(project: Path, name: str, direction: str, backend: str) -> Path:
    return migration_dir(project, 2) / f"{name}.{direction}.{backend}.sql"


def files_of(*steps: str) -> list[str]:
    return sorted(
        [
            f"{step}.{direction}.{backend}.sql"
            for step in steps
            for direction in ("up", "down")
            for backend in BOTH
        ]
        + ["ir.json"]
    )


def statuses(db, table: str = "enmorder") -> list[tuple]:
    return db.rows(f'SELECT "id", "status" FROM "{table}" ORDER BY "id"')


def _new_capturing(name: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run("migrate", "new", name)
    return code, out.getvalue(), err.getvalue()


# -- D1: a label added ------------------------------------------------------------------


@backend_matrix
def test_d1_an_added_label_is_a_first_labels_step_that_reverses_nothing(
    project, pkg, db
):
    start(project, pkg, db, models())
    db.execute('INSERT INTO "enmorder" ("status") VALUES (\'paid\')')
    write_models(project, pkg, models(("paid", "canceled", "refunded")))
    new("status_labels")

    assert step_files(project, 2) == files_of("01_labels")
    # The pass's label-addition statement, byte for byte (I-1 item 11).
    addition = json.loads(
        _core._plan_enum_label_addition(
            "enmorderstatus", ["paid", "canceled", "refunded"], ["paid", "canceled"]
        )
    )
    assert addition["statements"] == [
        "ALTER TYPE \"enmorderstatus\" ADD VALUE IF NOT EXISTS 'refunded'"
    ]
    assert (
        statements(step_file(project, "01_labels", "up", "postgres"))
        == addition["statements"]
    )
    assert step_file(project, "01_labels", "down", "postgres").read_text() == (
        "-- ferro: nothing-to-reverse Postgres cannot drop an enum label; "
        "'refunded' stays\n"
    )
    for direction in ("up", "down"):
        assert (
            step_file(project, "01_labels", direction, "sqlite").read_text()
            == "-- ferro: not-applicable\n"
        )

    assert run("migrate", "up", "--url", db.url) == 0
    db.execute('INSERT INTO "enmorder" ("status") VALUES (\'refunded\')')
    assert statuses(db) == [(1, "paid"), (2, "refunded")]
    assert plan_against(db, snapshot(project, 2), snapshot(project, 1)) == []

    # The down runs nothing and removes the record; the label stays, and an
    # extra label is warned about, never acted on: no drift.
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert keys(db) == [(1, 1)]
    assert plan_against(db, snapshot(project, 1), snapshot(project, 2)) == []
    assert statuses(db) == [(1, "paid"), (2, "refunded")]


@backend_matrix
def test_d1_a_label_and_a_new_column_of_its_type_are_labels_then_schema(
    project, pkg, db
):
    start(project, pkg, db, models())
    extra = "    previous: EnmOrderStatus | None = None\n"
    write_models(project, pkg, models(("paid", "canceled", "refunded"), extra=extra))
    new("status_previous")

    assert step_files(project, 2) == files_of("01_labels", "02_schema")
    schema = statements(step_file(project, "02_schema", "up", db.backend))
    assert schema[-1].startswith('ALTER TABLE "enmorder" ADD COLUMN "previous"')
    assert not any("CREATE TYPE" in sql for sql in schema)

    assert run("migrate", "up", "--url", db.url) == 0
    db.execute(
        'INSERT INTO "enmorder" ("status", "previous") VALUES (\'paid\', \'refunded\')'
    )
    round_trip(project, db)


# -- D3: a label renamed ----------------------------------------------------------------


RENAMED = models(("paid", "cancelled"), hint='{"cancelled": "canceled"}')


@backend_matrix
def test_d3_a_renamed_label_relabels_every_row_both_ways(project, pkg, db):
    start(project, pkg, db, models())
    db.execute(
        "INSERT INTO \"enmorder\" (\"status\") VALUES ('canceled'), ('paid'), ('canceled')"
    )
    db.execute('INSERT INTO "enmrefund" ("status") VALUES (\'canceled\')')
    write_models(project, pkg, RENAMED)
    new("rename_label")

    assert step_files(project, 2) == files_of("01_schema")
    assert statements(step_file(project, "01_schema", "up", "postgres")) == [
        "ALTER TYPE \"enmorderstatus\" RENAME VALUE 'canceled' TO 'cancelled'"
    ]
    assert statements(step_file(project, "01_schema", "down", "postgres")) == [
        "ALTER TYPE \"enmorderstatus\" RENAME VALUE 'cancelled' TO 'canceled'"
    ]
    # SQLite stores the label in the rows: an UPDATE of every column of the
    # type, and the reverse UPDATE as the down. The column is text as wide as
    # its longest label, so `cancelled` also widens it: a rebuild follows the
    # UPDATEs, and narrows it back on the down.
    assert statements(step_file(project, "01_schema", "up", "sqlite"))[:2] == [
        'UPDATE "enmorder" SET "status" = \'cancelled\' WHERE "status" = \'canceled\'',
        'UPDATE "enmrefund" SET "status" = \'cancelled\' WHERE "status" = \'canceled\'',
    ]
    assert statements(step_file(project, "01_schema", "down", "sqlite"))[:2] == [
        'UPDATE "enmorder" SET "status" = \'canceled\' WHERE "status" = \'cancelled\'',
        'UPDATE "enmrefund" SET "status" = \'canceled\' WHERE "status" = \'cancelled\'',
    ]

    round_trip(project, db)
    assert statuses(db) == [(1, "cancelled"), (2, "paid"), (3, "cancelled")]
    assert statuses(db, "enmrefund") == [(1, "cancelled")]
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert statuses(db) == [(1, "canceled"), (2, "paid"), (3, "canceled")]
    assert statuses(db, "enmrefund") == [(1, "canceled")]
    assert run("migrate", "up", "--url", db.url) == 0

    # Left in place after its migration, the hint is inert.
    code, out, _ = _new_capturing("again")
    assert (code, out) == (0, "no schema change: nothing written\n")


def test_d3_a_hint_whose_old_label_is_still_declared_is_refused_naming_it(project, pkg):
    still = models(("paid", "canceled", "cancelled"), hint='{"cancelled": "canceled"}')
    err = refused(project, pkg, "postgres", models(), still)
    assert "rename hint refused" in err
    assert '"enmorderstatus"' in err
    assert '__ferro_renamed_labels__ {"cancelled": "canceled"}' in err
    assert 'still declares the label "canceled"' in err


# -- D4: an enum class renamed ------------------------------------------------------------


@backend_matrix
def test_d4_a_renamed_enum_class_renames_its_type(project, pkg, db):
    start(project, pkg, db, models())
    db.execute('INSERT INTO "enmorder" ("status") VALUES (\'canceled\')')
    write_models(project, pkg, models(cls="EnmOrderState"))
    new("rename_type")

    assert step_files(project, 2) == files_of("01_schema")
    assert statements(step_file(project, "01_schema", "up", "postgres")) == [
        'ALTER TYPE "enmorderstatus" RENAME TO "enmorderstate"'
    ]
    assert statements(step_file(project, "01_schema", "down", "postgres")) == [
        'ALTER TYPE "enmorderstate" RENAME TO "enmorderstatus"'
    ]
    for direction in ("up", "down"):
        assert (
            step_file(project, "01_schema", direction, "sqlite").read_text()
            == "-- ferro: not-applicable\n"
        )

    round_trip(project, db)
    assert statuses(db) == [(1, "canceled")]
    if db.backend == "postgres":
        assert db.rows(
            "SELECT t.typname FROM pg_type t JOIN pg_namespace n "
            "ON n.oid = t.typnamespace WHERE n.nspname = current_schema() "
            "AND t.typtype = 'e' AND t.typname LIKE 'enmorder%' ORDER BY 1"
        ) == [("enmorderstate",)]


# -- B1: a new model and its enum type ------------------------------------------------


def _provenance(declaring: dict, added: list, inline: list, labels: dict) -> list:
    return json.loads(
        _core._plan_enum_type_provenance(
            json.dumps(declaring),
            json.dumps(added),
            json.dumps(inline),
            json.dumps(labels),
        )
    )


@backend_matrix
def test_b1_a_new_type_is_created_before_its_table_and_dropped_after_it(
    project, pkg, db
):
    start(project, pkg, db, models(refund=False))
    body = (
        models(refund=False)
        + """

class EnmShipKind(StrEnum):
    POST = "post"
    COURIER = "courier"


class EnmShipment(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    kind: EnmShipKind
"""
    )
    write_models(project, pkg, body)
    new("shipment")

    up = statements(step_file(project, "01_schema", "up", "postgres"))
    down = statements(step_file(project, "01_schema", "down", "postgres"))
    verdict = _provenance(
        {"enmshipkind": [["enmshipment", "kind"]]},
        [["enmshipment", "kind"]],
        [["enmshipment", "kind"]],
        {"enmshipkind": ["post", "courier"]},
    )
    assert [v["provenance"] for v in verdict] == ["introduced"]
    assert 'CREATE TYPE "enmshipkind"' in up[0]
    assert up[1].startswith('CREATE TABLE IF NOT EXISTS "enmshipment"')
    assert down == ['DROP TABLE "enmshipment"', verdict[0]["drop_statement"]]

    round_trip(project, db)


@backend_matrix
def test_b1_a_new_table_reusing_a_live_type_neither_creates_nor_drops_it(
    project, pkg, db
):
    start(project, pkg, db, models(refund=False))
    write_models(project, pkg, models())
    new("refund")

    verdict = _provenance(
        {"enmorderstatus": [["enmorder", "status"], ["enmrefund", "status"]]},
        [["enmrefund", "status"]],
        [["enmrefund", "status"]],
        {"enmorderstatus": ["paid", "canceled"]},
    )
    assert verdict == [
        {
            "name": "enmorderstatus",
            "provenance": "reused",
            "create_statement": None,
            "drop_statement": None,
        }
    ]
    for direction in ("up", "down"):
        text = step_file(project, "01_schema", direction, "postgres").read_text()
        assert "TYPE" not in text, text
    assert statements(step_file(project, "01_schema", "down", "postgres")) == [
        'DROP TABLE "enmrefund"'
    ]

    round_trip(project, db)


# -- the declaration ------------------------------------------------------------------


def test_the_hint_rides_every_column_of_its_enum_and_is_absent_when_undeclared(
    project, pkg
):
    write_config(project, pkg)
    write_models(project, pkg, models())
    new("create")
    write_models(project, pkg, RENAMED)
    new("rename_label")
    for number, expected in ((1, None), (2, {"cancelled": "canceled"})):
        columns = [
            column
            for model in snapshot(project, number)["payload"]["models"]
            for column in model["columns"]
            if column["name"] == "status"
        ]
        assert len(columns) == 2
        assert [c.get("enum_renamed_labels") for c in columns] == [expected] * 2


def test_a_hint_that_is_not_a_mapping_of_labels_fails_at_class_definition():
    from enum import StrEnum

    from ferro.ir.compiler import declared_renamed_labels

    class Good(StrEnum):
        __ferro_renamed_labels__ = {"b": "a", "a2": "z"}
        A2 = "a2"
        B = "b"

    class Bad(StrEnum):
        __ferro_renamed_labels__ = {"b": 1}
        B = "b"

    assert declared_renamed_labels(Good) == {"a2": "z", "b": "a"}
    assert declared_renamed_labels(None) == {}
    with pytest.raises(TypeError, match=r"Bad\.__ferro_renamed_labels__ must map"):
        declared_renamed_labels(Bad)
