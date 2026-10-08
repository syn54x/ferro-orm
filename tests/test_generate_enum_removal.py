# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""A label removed from a ``StrEnum``, generated end to end (#536, D2;
ADR-0011, ADR-0025, ADR-0033, ADR-0037, ADR-0040).

```python
class RmlOrderStatus(StrEnum):
    PAID = "paid"
    # CANCELED = "canceled"     # removed
    REFUNDED = "refunded"
```

```text
$ ferro migrate new drop_canceled
  0002_drop_canceled/
    01_backfill_rmlorder.py   @chunked(…status == "canceled"…)
                              rmlorder.status = todo("the label to use instead of 'canceled'")
    02_contract               Postgres: CREATE TYPE "rmlorderstatus_new" …; ALTER COLUMN … TYPE
                              "rmlorderstatus_new" USING "status"::text::"rmlorderstatus_new";
                              DROP TYPE "rmlorderstatus"; ALTER TYPE … RENAME TO "rmlorderstatus"
                              (its down: ADD VALUE IF NOT EXISTS 'canceled' AFTER 'paid')
                              SQLite: not-applicable (labels are text in the rows)
```

Every project targets both dialects and runs against the parametrized
database: rows hold the removed label, ``up`` refuses the unwritten ``todo``,
applies once it is written, the rows are relabelled, the live schema has no
drift against the new snapshot, ``down`` restores the label with no drift
against the parent. Table and type names start with ``rml`` so this module
never shares a name with another suite on the same Postgres server.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_migrate_down import (  # noqa: F401 - fixtures
    keys,
    migration_dir,
    plan_against,
    snapshot,
)
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    listing,
    pkg,
    project,
    run,
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
ALL = ("paid", "canceled", "refunded")
KEPT = ("paid", "refunded")
TODO = "todo(\"the label to use instead of 'canceled'\")"


@pytest.fixture
def no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write no ``.pyc``: a rewritten ``models.py`` of the same size inside
    the same second would otherwise import the stale one."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


def models(labels: tuple[str, ...], refund: bool = False, default: str = "") -> str:
    """``RmlOrder`` (and, when ``refund``, ``RmlRefund`` sharing its enum)."""
    members = "".join(f'    {label.upper()} = "{label}"\n' for label in labels)
    status = f"RmlOrderStatus{default}"
    refund_model = (
        """

class RmlRefund(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    status: RmlOrderStatus
"""
        if refund
        else ""
    )
    return (
        f"""
class RmlOrderStatus(StrEnum):
{members}

class RmlOrder(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    status: {status}
"""
        + refund_model
    )


def start(project: Path, pkg: str, db, body: str) -> None:
    """``0001`` creates ``body``'s models for both dialects and is applied."""
    write_config(project, pkg)
    write_models(project, pkg, body)
    new("create")
    assert run("migrate", "up", "--url", db.url) == 0


def seed(db, table: str, labels: list[str]) -> None:
    values = ", ".join(f"('{label}')" for label in labels)
    db.execute(f'INSERT INTO "{table}" ("status") VALUES {values}')


def statuses(db, table: str = "rmlorder") -> list[tuple]:
    return db.rows(f'SELECT "id", CAST("status" AS TEXT) FROM "{table}" ORDER BY "id"')


def text(migration: Path, file: str) -> str:
    return (migration / file).read_text()


def ddl_files(*steps: str) -> list[str]:
    return [
        f"{step}.{direction}.{dialect}.sql"
        for step in steps
        for direction in ("up", "down")
        for dialect in BOTH
    ]


def write_value(migration: Path, step: str, value: str) -> None:
    path = migration / step
    source = path.read_text()
    assert TODO in source, source
    path.write_text(source.replace(TODO, value))


def labels_of(db, type_name: str = "rmlorderstatus") -> list[str]:
    return [
        row[0]
        for row in db.rows(
            "SELECT e.enumlabel::text FROM pg_enum e JOIN pg_type t "
            "ON t.oid = e.enumtypid JOIN pg_namespace n ON n.oid = t.typnamespace "
            f"WHERE n.nspname = current_schema() AND t.typname = '{type_name}' "
            "ORDER BY e.enumsortorder"
        )
    ]


def clean(db, project: Path, number: int, other: int) -> bool:
    return plan_against(db, snapshot(project, number), snapshot(project, other)) == []


SWAP = [
    "CREATE TYPE \"rmlorderstatus_new\" AS ENUM ('paid', 'refunded')",
    'ALTER TABLE "rmlorder" ALTER COLUMN "status" TYPE "rmlorderstatus_new" '
    'USING "status"::text::"rmlorderstatus_new"',
    'DROP TYPE "rmlorderstatus"',
    'ALTER TYPE "rmlorderstatus_new" RENAME TO "rmlorderstatus"',
]


# -- D2: a label removed --------------------------------------------------------------


@backend_matrix
def test_d2_a_removed_label_is_backfilled_then_contracted_and_round_trips(
    project, pkg, db, capsys
):
    start(project, pkg, db, models(ALL))
    seed(db, "rmlorder", ["paid", "canceled", "refunded", "canceled"])
    write_models(project, pkg, models(KEPT))
    capsys.readouterr()
    new("drop_canceled")
    out = capsys.readouterr().out
    assert "removed enum labels: rmlorderstatus.canceled" in out, out
    migration = migration_dir(project, 2)

    assert listing(migration) == sorted(
        ddl_files("02_contract") + ["01_backfill_rmlorder.py", "ir.json"]
    )
    backfill = text(migration, "01_backfill_rmlorder.py")
    assert (
        "@chunked(\n"
        "    lambda models: models.RmlOrder.where("
        'lambda rmlorder: rmlorder.status == "canceled")\n'
        "    .order_by(lambda rmlorder: rmlorder.id),\n"
        "    batch_size=1000,\n"
        ")\n"
        "async def up(ctx, batch):\n"
        "    for rmlorder in batch:\n"
        f"        rmlorder.status = type(rmlorder.status)({TODO})\n"
        "        await rmlorder.save()\n"
    ) in backfill, backfill
    assert (
        '@nothing_to_reverse("the contract\'s down restores the label")\n'
        "def down(ctx): ...\n"
    ) in backfill
    assert "--no-backfill rmlorder.status" in backfill
    assert text(migration, "02_contract.up.postgres.sql") == (
        "-- ferro: data-dependent\n\n" + "\n\n".join(f"{s};" for s in SWAP) + "\n"
    )
    assert text(migration, "02_contract.down.postgres.sql") == (
        "ALTER TYPE \"rmlorderstatus\" ADD VALUE IF NOT EXISTS 'canceled' AFTER 'paid';\n"
    )
    for direction in ("up", "down"):
        assert (
            text(migration, f"02_contract.{direction}.sqlite.sql")
            == "-- ferro: not-applicable\n"
        )

    # The unwritten label refuses the migration before anything runs.
    assert run("migrate", "up", "--url", db.url) == 1
    err = capsys.readouterr().err
    assert "01_backfill_rmlorder.py:" in err, err
    assert "not written yet: the label to use instead of 'canceled'" in err, err
    assert keys(db) == [(1, 1)]

    write_value(migration, "01_backfill_rmlorder.py", '"refunded"')
    assert run("migrate", "up", "--url", db.url) == 0
    assert keys(db) == [(1, 1), (2, 1), (2, 2)]
    assert statuses(db) == [
        (1, "paid"),
        (2, "refunded"),
        (3, "refunded"),
        (4, "refunded"),
    ]
    if db.backend == "postgres":
        assert labels_of(db) == ["paid", "refunded"]
    assert clean(db, project, 2, 1)

    # The down puts the label back (Postgres) or has nothing to reverse
    # (SQLite, text storage): no drift against the parent.
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert keys(db) == [(1, 1)]
    if db.backend == "postgres":
        # Where the parent declares it, not appended: the type's order (its
        # comparisons, ``ORDER BY status``) is the parent's again.
        assert labels_of(db) == list(ALL)
        assert db.rows("SELECT enum_range(NULL::rmlorderstatus)::text") == [
            ("{paid,canceled,refunded}",)
        ]
    assert clean(db, project, 1, 2)
    seed(db, "rmlorder", ["canceled"])
    assert run("migrate", "up", "--url", db.url) == 0
    assert statuses(db)[-1] == (5, "refunded")
    assert clean(db, project, 2, 1)


@pytest.mark.postgres_only
def test_d2_a_type_two_tables_share_is_one_swap_over_both_columns(
    project, pkg, db, capsys
):
    start(project, pkg, db, models(ALL, refund=True))
    seed(db, "rmlorder", ["canceled", "paid"])
    seed(db, "rmlrefund", ["canceled"])
    write_models(project, pkg, models(KEPT, refund=True))
    new("drop_canceled")
    migration = migration_dir(project, 2)

    assert listing(migration) == sorted(
        ddl_files("03_contract")
        + ["01_backfill_rmlorder.py", "02_backfill_rmlrefund.py", "ir.json"]
    )
    refund_swap = (
        'ALTER TABLE "rmlrefund" ALTER COLUMN "status" TYPE "rmlorderstatus_new" '
        'USING "status"::text::"rmlorderstatus_new"'
    )
    up = text(migration, "03_contract.up.postgres.sql")
    assert up == (
        "-- ferro: data-dependent\n\n"
        + "\n\n".join(f"{s};" for s in [*SWAP[:2], refund_swap, *SWAP[2:]])
        + "\n"
    )
    for step in ("01_backfill_rmlorder.py", "02_backfill_rmlrefund.py"):
        write_value(migration, step, '"paid"')
    assert run("migrate", "up", "--url", db.url) == 0
    assert statuses(db) == [(1, "paid"), (2, "paid")]
    assert statuses(db, "rmlrefund") == [(1, "paid")]
    assert labels_of(db) == ["paid", "refunded"]
    assert clean(db, project, 2, 1)
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert clean(db, project, 1, 2)


@pytest.mark.postgres_only
def test_d2_a_column_default_is_dropped_around_the_swap_and_set_again(project, pkg, db):
    start(project, pkg, db, models(ALL, default=" = RmlOrderStatus.PAID"))
    seed(db, "rmlorder", ["canceled"])
    write_models(project, pkg, models(KEPT, default=" = RmlOrderStatus.PAID"))
    new("drop_canceled")
    migration = migration_dir(project, 2)
    alter = 'ALTER TABLE "rmlorder" ALTER COLUMN "status"'
    up = text(migration, "02_contract.up.postgres.sql")
    assert f"{alter} DROP DEFAULT;" in up and f"{alter} SET DEFAULT 'paid';" in up
    write_value(migration, "01_backfill_rmlorder.py", '"paid"')
    assert run("migrate", "up", "--url", db.url) == 0
    db.execute('INSERT INTO "rmlorder" DEFAULT VALUES')
    assert statuses(db) == [(1, "paid"), (2, "paid")]
    assert clean(db, project, 2, 1)


@pytest.mark.postgres_only
def test_d2_a_row_written_behind_the_backfill_fails_the_contract_with_the_count_and_recipe(
    project, pkg, db, capsys
):
    start(project, pkg, db, models(ALL))
    seed(db, "rmlorder", ["canceled"])
    write_models(project, pkg, models(KEPT))
    new("drop_canceled")
    migration = migration_dir(project, 2)
    # A backfill that relabels nothing stands in for rows written behind it.
    write_value(migration, "01_backfill_rmlorder.py", "rmlorder.status")
    capsys.readouterr()
    assert run("migrate", "up", "--url", db.url) == 1
    err = capsys.readouterr().err
    assert (
        '1 row still holds \'canceled\' in "status" of "rmlorder"; run ferro migrate '
        "down --to 0001 then ferro migrate up to re-run the backfill"
    ) in err, err


# -- D2 with --no-backfill ------------------------------------------------------------


@backend_matrix
def test_d2_no_backfill_writes_a_guard_that_passes_empty_and_fails_naming_the_count(
    project, pkg, db, capsys
):
    start(project, pkg, db, models(ALL))
    seed(db, "rmlorder", ["paid"])
    write_models(project, pkg, models(KEPT))
    new("drop_canceled", "--no-backfill", "rmlorder.status")
    migration = migration_dir(project, 2)

    assert listing(migration) == sorted(
        ddl_files("02_contract") + ["01_guard_rmlorder.py", "ir.json"]
    )
    guard = text(migration, "01_guard_rmlorder.py")
    assert 'lambda rmlorder: rmlorder.status == "canceled"' in guard, guard
    # No row holds the label: the guard passes and the contract applies.
    assert run("migrate", "up", "--url", db.url) == 0
    assert clean(db, project, 2, 1)
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert clean(db, project, 1, 2)

    seed(db, "rmlorder", ["canceled", "canceled"])
    capsys.readouterr()
    assert run("migrate", "up", "--url", db.url) == 1
    err = capsys.readouterr().err
    assert "01_guard_rmlorder.py failed" in err, err
    assert "2 rmlorder rows still hold 'canceled' in status" in err, err
    # The guard's failed record stands; the contract never ran.
    assert keys(db) == [(1, 1), (2, 1)]


# -- D2 on an enum stored as text with a db_check ---------------------------------------


def checked_models(labels: tuple[str, ...]) -> str:
    members = "".join(f'    {label.upper()} = "{label}"\n' for label in labels)
    return f"""
class RmlOrderStatus(StrEnum):
{members}

class RmlOrder(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    status: Annotated[RmlOrderStatus, FerroField(db_type="text", db_check=True)]
"""


@backend_matrix
def test_d2_a_text_enums_check_is_rebuilt_to_the_labels_left(project, pkg, db):
    start(project, pkg, db, checked_models(ALL))
    seed(db, "rmlorder", ["canceled", "paid"])
    write_models(project, pkg, checked_models(KEPT))
    new("drop_canceled")
    migration = migration_dir(project, 2)

    assert listing(migration) == sorted(
        ddl_files("02_contract") + ["01_backfill_rmlorder.py", "ir.json"]
    )
    pg_up = text(migration, "02_contract.up.postgres.sql")
    assert "TYPE" not in pg_up
    assert (
        'ALTER TABLE "rmlorder" ADD CONSTRAINT "ck_rmlorder_status" '
        "CHECK (\"status\" IN ('paid', 'refunded'));"
    ) in pg_up, pg_up
    write_value(migration, "01_backfill_rmlorder.py", '"paid"')
    assert run("migrate", "up", "--url", db.url) == 0
    assert statuses(db) == [(1, "paid"), (2, "paid")]
    assert clean(db, project, 2, 1)
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert clean(db, project, 1, 2)


# -- the Alembic bridge never meets a removal ---------------------------------------


def test_the_alembic_translator_refuses_a_removal_naming_the_op():
    from ferro.migrations.translate import translate

    plan = {
        "target": {
            "ir_kind": "schema",
            "ir_version": 2,
            "payload": {"dialect_agnostic": True, "models": []},
        },
        "dialect": "postgres",
        "operations": [
            {
                "kind": "RemoveEnumLabel",
                "type_name": "rmlorderstatus",
                "label": "canceled",
                "columns": [],
                "statements": [],
                "reports": [],
            }
        ],
    }
    with pytest.raises(RuntimeError, match="a RemoveEnumLabel op, which only two"):
        translate(plan, direction="up")
