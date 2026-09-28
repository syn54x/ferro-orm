"""``op.execute`` rendering of Rust-rendered statements (#449).

Alembic executes ``op.execute('<str>')`` through ``sqlalchemy.text()``, which
reads ``:word`` as a bind parameter. Every statement ferro writes into a
revision — enum type creation, label addition, checks, row security — is
rendered by the Rust core and may carry a user's own literals, so an enum
label ``':admin'`` used to fail the upgrade with "A value is required for
bind parameter admin". The bridge renders every such statement as
``sa.DDL(...)``, which is never bind-parsed, so the SQL that reaches
Postgres is byte-identical to the runtime's (AGENTS.md § I-1).
"""

from __future__ import annotations

from enum import StrEnum

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import ferro
from ferro import Field, Model, clear_registry, connect, reset_engine
from ferro.raw import execute
from ferro.session import engines
from tests._alembic_harness import (
    assert_statement_in_code,
    autogen_upgrade_and_downgrade_code,
    autogen_upgrade_code,
    run_generated_code,
    sync_url,
)


def _rewind_registry() -> None:
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


@pytest.fixture(autouse=True)
def cleanup_registry():
    _rewind_registry()
    yield
    _rewind_registry()


# Labels that break bind parsing (``:admin``, ``/users/:id``), that the
# backslash escape cannot express (``:smile:``), casts, a pre-escaped colon,
# percent signs in every shape the driver or ``%``-formatting could read,
# dollar quoting, quotes, a bare backslash, and non-ASCII.
LABELS = [
    ":admin",
    "/users/:id",
    ":smile:",
    ":ab:",
    "^x:[0-9]+:",
    "a:",
    ":a:b",
    "x::y",
    "\\:x",
    "100%",
    "%%",
    "%(name)s",
    "%s",
    "$$",
    "it's",
    "é:ü",
    ":$",
    ":-",
    "::",
    ":",
    "\\",
]


def _rendered_argument(rendered: str):
    """The ``sa.DDL(...)`` object a revision file builds from the rendered
    line, evaluated the way the file would."""
    assert rendered.startswith("op.execute(sa.DDL(") and rendered.endswith("))")
    return eval(rendered[len("op.execute(") : -1], {"sa": sa})


@pytest.mark.sqlite_only
def test_rendered_execute_is_a_ddl_construct_with_percent_doubled() -> None:
    from ferro.migrations.alembic import _render_execute

    assert (
        _render_execute("SELECT ':admin'") == "op.execute(sa.DDL(\"SELECT ':admin'\"))"
    )
    assert _render_execute("LIKE '100%'") == "op.execute(sa.DDL(\"LIKE '100%%'\"))"
    policy = (
        'CREATE POLICY "rls_x_y" ON "x" USING '
        "(\"y\" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid)"
    )
    assert _rendered_argument(_render_execute(policy)).statement == policy


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.parametrize("label", LABELS)
def test_rendered_execute_reaches_postgres_byte_identical(
    label: str, postgres_base_url, db_schema_name
) -> None:
    """Executed through a real ``Operations`` the way a revision runs, the
    label Postgres stores is the label the Rust statement carried."""
    from ferro.migrations.alembic import _render_execute

    statement = 'CREATE TYPE "probe" AS ENUM (\'' + label.replace("'", "''") + "')"
    engine = sa.create_engine(sync_url(postgres_base_url))
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'SET search_path TO "{db_schema_name}"'))
            op = Operations(MigrationContext.configure(conn))
            op.execute(_rendered_argument(_render_execute(statement)))
            stored = conn.execute(
                sa.text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'probe'"
                )
            ).scalar()
            conn.rollback()
    finally:
        engine.dispose()
    assert stored == label


# ---------------------------------------------------------------------------
# The two enum doors the bug reached, end to end on Postgres
# ---------------------------------------------------------------------------


class Role(StrEnum):
    ADMIN = ":admin"
    USER_PATH = "/users/:id"
    SMILE = ":smile:"


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_add_column_type_creation_with_colon_labels_runs(
    db_url, postgres_base_url, db_schema_name
):
    """#439's type creation for a ``StrEnum`` whose values carry colons: the
    generated upgrade runs, stores the labels verbatim, and its downgrade
    runs."""

    class Member(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str | None = None

    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    class Member(Model):  # type: ignore[no-redef]  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        name: str | None = None
        role: Role | None = None

    await connect(db_url)

    upgrade_code, downgrade_code = autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    assert "op.execute(sa.DDL(" in upgrade_code, upgrade_code
    assert "op.add_column('member'" in upgrade_code, upgrade_code
    run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    async with engines.session():
        assert await _live_labels("role") == [":admin", "/users/:id", ":smile:"]
    assert_statement_in_code('DROP TYPE "role"', downgrade_code)
    run_generated_code(downgrade_code, postgres_base_url, db_schema_name)


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_label_addition_with_colon_labels_runs(
    db_url, postgres_base_url, db_schema_name
):
    """The pre-existing ``ADD VALUE`` path had the same hazard: a live type
    gains colon-bearing labels, and the generated revision runs and stores
    them verbatim."""
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TYPE \"role\" AS ENUM (':admin')")
        await execute(
            'CREATE TABLE "member" ("id" serial PRIMARY KEY, "role" "role" NOT NULL)'
        )
    reset_engine()

    class Member(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        role: Role

    code = autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "ADD VALUE IF NOT EXISTS" in code, code
    run_generated_code(code, postgres_base_url, db_schema_name)
    await connect(db_url)
    async with engines.session():
        assert await _live_labels("role") == [":admin", "/users/:id", ":smile:"]


async def _live_labels(type_name: str) -> list[str]:
    from ferro.raw import fetch_all

    rows = await fetch_all(
        "SELECT e.enumlabel AS label FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "JOIN pg_enum e ON e.enumtypid = t.oid "
        f"WHERE n.nspname = current_schema() AND t.typname = '{type_name}' "
        "ORDER BY e.enumsortorder"
    )
    return [r["label"] for r in rows]
