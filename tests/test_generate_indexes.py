# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""A redefined index and a removed foreign key, generated (ADR-0051).

```python
class SubscriptionInvoiceLine(Model):
    # was (("billing_period_start", "billing_period_end"),)
    __ferro_composite_indexes__ = (
        ("billing_period_start", "billing_period_end", "customer_id"),
    )
```

```text
$ ferro migrate new widen_lookup
  0002_widen_lookup/
    01_idx_subscriptioninvoiceline_billing_period_start_billing_pe_idx.up.postgres.sql
        -- ferro: no-transaction
        DROP INDEX CONCURRENTLY IF EXISTS "idx_subscriptioninvoiceline_billing_period_start_billing_pe_idx";
        CREATE INDEX CONCURRENTLY "idx_subscriptioninvoiceline_…" ON … ("billing_period_start", "billing_period_end", "customer_id");
```

Both column groups cut to the same 63-character name, so the change is a
redefinition under one name: its own index step, which drops the old
definition and builds the new one on both dialects. The planner plans the
same `RedefineIndex` the reconciliation pass, `drift` and the Alembic bridge
read. Every case applies ``0001``, generates the change, checks the files,
and round-trips ``up`` / ``down`` / ``up`` with no drift.
"""

from __future__ import annotations

import pytest

from tests import test_generate_sqlite_rebuild as sqlite_rebuild
from tests.test_generate_postgres_staging import (  # noqa: F401 - fixtures
    files,
    generate,
    no_bytecode,
    round_trip,
    start,
    text,
)
from tests.test_migrate_down import migration_dir  # noqa: F401
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    listing,
    pkg,
    project,
    run,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    db,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode"
)

backend_matrix = pytest.mark.backend_matrix


# -- models -------------------------------------------------------------------------

HEAD = "from typing import ClassVar\n"


def invoice_lines(*indexed: str) -> str:
    """``SubscriptionInvoiceLine`` with one composite index over ``indexed``."""
    columns = ", ".join(f'"{column}"' for column in indexed)
    return (
        HEAD
        + f"""

class SubscriptionInvoiceLine(Model):
    __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ({columns}),
    )
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    billing_period_start: int
    billing_period_end: int
    customer_id: int
"""
    )


def lines(*indexed: str) -> str:
    """``Line`` with one composite index over ``indexed``: ``("order_id",
    "kind")`` and ``("order", "id_kind")`` both join to
    ``idx_line_order_id_kind``."""
    columns = ", ".join(f'"{column}"' for column in indexed)
    return (
        HEAD
        + f"""

class Line(Model):
    __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ({columns}),
    )
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    order_id: int | None = None
    kind: str | None = None
    order: int | None = None
    id_kind: str | None = None
"""
    )


NARROW = invoice_lines("billing_period_start", "billing_period_end")
WIDE = invoice_lines("billing_period_start", "billing_period_end", "customer_id")
TRUNCATED = "idx_subscriptioninvoiceline_billing_period_start_billing_pe_idx"
JOINED = lines("order_id", "kind")
REJOINED = lines("order", "id_kind")
LINKED = sqlite_rebuild.members("Team")
"""``author.team_id`` with its foreign key to ``team``."""
UNLINKED = (
    sqlite_rebuild.TEAM
    + sqlite_rebuild.CLUB
    + AUTHOR
    + "    team_id: int | None = None\n"
)
"""The same column, no longer a foreign key."""
FK = "fk_author_team_id_team"


def built(name: str, table: str, columns: str, *, concurrent: bool) -> str:
    if concurrent:
        return (
            f'DROP INDEX CONCURRENTLY IF EXISTS "{name}";\n\n'
            f'CREATE INDEX CONCURRENTLY "{name}" ON "{table}" ({columns});\n'
        )
    return (
        f'DROP INDEX IF EXISTS "{name}";\n\n'
        f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({columns});\n'
    )


# -- a redefined index -------------------------------------------------------------


@backend_matrix
def test_a_cut_name_over_more_columns_is_its_own_index_step_both_ways(project, pkg, db):
    start(project, pkg, db, NARROW)

    migration = generate(project, pkg, WIDE, "widen_lookup")

    step = f"01_{TRUNCATED}"
    assert listing(migration) == files(migration, step)
    wide = '"billing_period_start", "billing_period_end", "customer_id"'
    narrow = '"billing_period_start", "billing_period_end"'
    table = "subscriptioninvoiceline"
    assert text(migration, step, "up", "postgres") == (
        "-- ferro: no-transaction\n\n" + built(TRUNCATED, table, wide, concurrent=True)
    )
    assert text(migration, step, "down", "postgres") == (
        "-- ferro: no-transaction\n\n"
        + built(TRUNCATED, table, narrow, concurrent=True)
    )
    # SQLite drops the old definition first: `IF NOT EXISTS` would keep it.
    assert text(migration, step, "up", "sqlite") == built(
        TRUNCATED, table, wide, concurrent=False
    )
    assert text(migration, step, "down", "sqlite") == built(
        TRUNCATED, table, narrow, concurrent=False
    )

    round_trip(project, db, steps=1)


@backend_matrix
def test_an_underscore_join_collision_is_its_own_index_step_both_ways(project, pkg, db):
    start(project, pkg, db, JOINED)

    migration = generate(project, pkg, REJOINED, "rejoin_lookup")

    step = "01_idx_line_order_id_kind"
    assert listing(migration) == files(migration, step)
    assert text(migration, step, "up", "sqlite") == built(
        "idx_line_order_id_kind", "line", '"order", "id_kind"', concurrent=False
    )
    assert text(migration, step, "down", "sqlite") == built(
        "idx_line_order_id_kind", "line", '"order_id", "kind"', concurrent=False
    )

    round_trip(project, db, steps=1)


# -- a removed foreign key -----------------------------------------------------------


@backend_matrix
def test_a_foreign_key_removed_from_a_kept_column_is_dropped_and_its_down_adds_it(
    project, pkg, db
):
    """``team: Annotated[Team, ForeignKey(...)]`` becomes ``team_id: int``: the
    column stays, its constraint goes. No door planned it before (ADR-0051)."""
    start(project, pkg, db, LINKED)

    migration = generate(project, pkg, UNLINKED, "unlink_team")

    assert listing(migration) == files(migration, "01_schema")
    assert text(migration, "01_schema", "up", "postgres") == (
        f'ALTER TABLE "author" DROP CONSTRAINT "{FK}";\n'
    )
    # Rows written since may reference no team: putting it back can fail.
    assert text(migration, "01_schema", "down", "postgres") == (
        "-- ferro: data-dependent\n\n"
        f'ALTER TABLE "author" ADD CONSTRAINT "{FK}" FOREIGN KEY ("team_id") '
        'REFERENCES "team" ("id") ON DELETE SET NULL;\n'
    )
    # SQLite drops a table constraint by rebuilding the table, both ways.
    for direction in ("up", "down"):
        assert text(migration, "01_schema", direction, "sqlite").startswith(
            sqlite_rebuild.REBUILD
        )
    assert FK not in text(migration, "01_schema", "up", "sqlite")
    assert FK in text(migration, "01_schema", "down", "sqlite")

    round_trip(project, db, steps=1)
