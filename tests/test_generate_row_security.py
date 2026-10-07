# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate new`` for row security on an existing table (#531; ADR-0019,
ADR-0033).

```python
class RlsOrder(Model):
    __ferro_rls__ = RowSecurity(RowPolicy(column="tenant_id", setting="app.tenant"))
```

```text
0002_order_rls/
  01_schema.up.postgres.sql     ALTER TABLE "rlsorder" ENABLE ROW LEVEL SECURITY;
                                ALTER TABLE "rlsorder" FORCE ROW LEVEL SECURITY;
                                CREATE POLICY "rls_rlsorder_tenant_id" ON "rlsorder" … USING (…);
  01_schema.down.postgres.sql   DROP POLICY "rls_rlsorder_tenant_id" ON "rlsorder";
                                ALTER TABLE "rlsorder" NO FORCE ROW LEVEL SECURITY;
                                ALTER TABLE "rlsorder" DISABLE ROW LEVEL SECURITY;
  01_schema.up.sqlite.sql       -- ferro: not-applicable
```

Every project targets both dialects; each test applies the migration to the
parametrized database, checks there is no drift, reverts it, checks there is
no drift against the parent, and on Postgres reads the table through a
non-bypassing login role to see the policy take effect. Every Postgres
statement is pinned to the pass's own for the same declaration (I-1 items
15–16). Tables start with ``rls`` and roles with ``rlsgen_`` so this module
never shares a name with another suite on the same Postgres server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

import ferro
from ferro import _core
from tests.test_generate_columns import (  # noqa: F401 - fixtures
    _new_capturing,
    no_bytecode,
    round_trip,
)
from tests.test_migrate_down import (  # noqa: F401 - fixtures
    migration_dir,
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
from tests.test_rls_end_to_end import TENANT_PASSWORD, _tenant_url

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode"
)

backend_matrix = pytest.mark.backend_matrix

BOTH = ("postgres", "sqlite")
TABLE = "rlsorder"
TENANT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NOT_APPLICABLE = "-- ferro: not-applicable\n"

TENANT = 'RowPolicy(column="tenant_id", setting="{setting}")'
OWNER = 'RowPolicy(name="owner", command="select", column="owner", setting="app.owner", restrictive=True)'


def models(*policies: str, declared: bool = True, setting: str = "app.tenant") -> str:
    """``RlsOrder``, under a row-security declaration of ``policies`` (the
    tenant policy when none is given) unless ``declared`` is false."""
    rls = ""
    if declared:
        listed = ", ".join(policies or (TENANT.format(setting=setting),))
        rls = f"    __ferro_rls__ = RowSecurity({listed})\n"
    return f"""
import uuid


class RlsOrder(Model):
{rls}    id: Annotated[int | None, FerroField(primary_key=True)] = None
    tenant_id: uuid.UUID
    owner: str
"""


def start(project: Path, pkg: str, db, body: str) -> None:
    """``0001`` creates ``body``'s model for both dialects, is applied, and
    the table holds two rows of tenant A and one of tenant B."""
    write_config(project, pkg)
    write_models(project, pkg, body)
    new("create")
    assert run("migrate", "up", "--url", db.url) == 0
    db.execute(
        f'INSERT INTO "{TABLE}" ("tenant_id", "owner") VALUES '
        f"('{TENANT_A}', 'ann'), ('{TENANT_A}', 'bob'), ('{TENANT_B}', 'cyd')"
    )


def step_file(project: Path, number: int, direction: str, backend: str) -> Path:
    return migration_dir(project, number) / f"01_schema.{direction}.{backend}.sql"


def generated(project: Path, pkg: str, body: str) -> tuple[list[str], list[str]]:
    """Generate ``0002`` for ``body``: one schema step, SQLite not-applicable
    both ways. Returns the Postgres up and down statements."""
    write_models(project, pkg, body)
    new("order_rls")
    assert sorted(p.name for p in migration_dir(project, 2).iterdir()) == sorted(
        [f"01_schema.{d}.{b}.sql" for d in ("up", "down") for b in BOTH] + ["ir.json"]
    )
    for direction in ("up", "down"):
        assert step_file(project, 2, direction, "sqlite").read_text() == NOT_APPLICABLE
    return (
        statements(step_file(project, 2, "up", "postgres")),
        statements(step_file(project, 2, "down", "postgres")),
    )


def model_ir(project: Path, number: int) -> dict:
    return next(
        model
        for model in snapshot(project, number)["payload"]["models"]
        if model["table_name"] == TABLE
    )


def live_row_security(db) -> dict:
    """The table's row security as the runtime pass reads it live."""

    async def read() -> dict:
        name = f"rls_live_{uuid.uuid4().hex}"
        await ferro.connect(db.url, name=name)
        try:
            _, facts = await _core._live_schema_ir(name, json.dumps([TABLE]))
        finally:
            await _core._disconnect(name)
        return json.loads(facts)["tables"][TABLE]["row_security"]

    return asyncio.run(read())


def reconcile(project: Path, db) -> list[str]:
    """What the pass executes for ``0002``'s declaration over the live table,
    ``migrate_destructive`` (the generator plans every drop)."""
    plan = _core._plan_row_security_reconcile(
        json.dumps(model_ir(project, 2)),
        json.dumps(live_row_security(db)),
        "postgres",
        True,
    )
    return json.loads(plan)["statements"]


@contextlib.contextmanager
def tenant_role(db) -> Iterator[str]:
    """A NOSUPERUSER, non-BYPASSRLS login role that may read the table."""
    role = f"rlsgen_{uuid.uuid4().hex[:12]}"
    try:
        db.execute(
            f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOBYPASSRLS '
            f"PASSWORD '{TENANT_PASSWORD}'"
        )
        db.execute(f'GRANT USAGE ON SCHEMA "{db.schema}" TO "{role}"')
        db.execute(f'GRANT SELECT ON "{TABLE}" TO "{role}"')
        yield role
    finally:
        db.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE usename = '{role}' AND pid <> pg_backend_pid()"
        )
        with contextlib.suppress(Exception):
            db.execute(f'DROP OWNED BY "{role}"')
        db.execute(f'DROP ROLE IF EXISTS "{role}"')


def visible(db, role: str, settings: dict[str, str]) -> list[str]:
    """The owners ``role`` sees with ``settings`` set, on its own connection."""
    import psycopg

    conn = psycopg.connect(_tenant_url(db.base, role), autocommit=True)
    try:
        conn.execute(f'SET search_path TO "{db.schema}"')
        for key, value in settings.items():
            conn.execute("SELECT set_config(%s, %s, false)", (key, value))
        rows = conn.execute(f'SELECT "owner" FROM "{TABLE}" ORDER BY "owner"')
        return [row[0] for row in rows.fetchall()]
    finally:
        conn.close()


def enable() -> str:
    return f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY'


def force() -> str:
    return f'ALTER TABLE "{TABLE}" FORCE ROW LEVEL SECURITY'


def no_force() -> str:
    return f'ALTER TABLE "{TABLE}" NO FORCE ROW LEVEL SECURITY'


def disable() -> str:
    return f'ALTER TABLE "{TABLE}" DISABLE ROW LEVEL SECURITY'


def drop_policy(name: str) -> str:
    return f'DROP POLICY "{name}" ON "{TABLE}"'


TENANT_POLICY = "rls_rlsorder_tenant_id"
OWNER_POLICY = "rls_rlsorder_owner"


def create_policy(statements_: list[str], name: str) -> str:
    return next(s for s in statements_ if s.startswith(f'CREATE POLICY "{name}"'))


# -- E1: row security added to an existing table --------------------------------------


@backend_matrix
def test_e1_added_row_security_enables_forces_creates_and_its_down_tears_it_down(
    project, pkg, db
):
    start(project, pkg, db, models(declared=False))
    up, down = generated(project, pkg, models())

    # The create pass's statements for the declaration, byte for byte.
    create = json.loads(_core._plan_row_security(json.dumps(model_ir(project, 2))))
    assert up == create["statements"]
    assert up[:2] == [enable(), force()]
    assert (
        "USING (\"tenant_id\" = NULLIF(current_setting('app.tenant', true), '')::uuid)"
        in up[2]
    )
    assert down == [drop_policy(TENANT_POLICY), no_force(), disable()]
    if db.backend == "postgres":
        assert up == reconcile(project, db)

    round_trip(project, db)
    if db.backend == "postgres":
        with tenant_role(db) as role:
            assert visible(db, role, {"app.tenant": TENANT_A}) == ["ann", "bob"]
            assert visible(db, role, {"app.tenant": TENANT_B}) == ["cyd"]
            assert visible(db, role, {}) == []
            # Reverted, the table filters nothing again.
            assert run("migrate", "down", "--yes", "--url", db.url) == 0
            assert visible(db, role, {}) == ["ann", "bob", "cyd"]


# -- B1: a new table with row security ------------------------------------------------


def test_b1_a_new_table_creates_its_row_security_after_the_table_and_drops_the_table_alone(
    project, pkg
):
    write_config(project, pkg)
    write_models(project, pkg, models())
    new("create")
    up = statements(step_file(project, 1, "up", "postgres"))
    at = next(i for i, s in enumerate(up) if s.startswith("CREATE TABLE"))
    assert (
        up[at + 1 :]
        == json.loads(_core._plan_row_security(json.dumps(model_ir(project, 1))))[
            "statements"
        ]
    )
    assert statements(step_file(project, 1, "down", "postgres")) == [
        f'DROP TABLE "{TABLE}"'
    ]
    assert "POLICY" not in step_file(project, 1, "up", "sqlite").read_text()


# -- E2: a policy body changed ---------------------------------------------------------


@backend_matrix
def test_e2_a_changed_body_is_dropped_and_recreated_and_its_down_restores_the_old_one(
    project, pkg, db
):
    start(project, pkg, db, models())
    old_create = create_policy(
        statements(step_file(project, 1, "up", "postgres")), TENANT_POLICY
    )
    up, down = generated(project, pkg, models(setting="app.tenant_id"))

    new_create = json.loads(_core._plan_row_security(json.dumps(model_ir(project, 2))))[
        "statements"
    ][-1]
    assert "current_setting('app.tenant_id', true)" in new_create
    assert up == [drop_policy(TENANT_POLICY), new_create]
    assert down == [drop_policy(TENANT_POLICY), old_create]
    if db.backend == "postgres":
        assert up == reconcile(project, db)

    round_trip(project, db)
    if db.backend == "postgres":
        with tenant_role(db) as role:
            assert visible(db, role, {"app.tenant_id": TENANT_B}) == ["cyd"]
            assert visible(db, role, {"app.tenant": TENANT_B}) == []


# -- E3: row security removed -----------------------------------------------------------


@backend_matrix
def test_e3_removed_row_security_is_dropped_and_torn_down_and_its_down_recreates_it(
    project, pkg, db
):
    start(project, pkg, db, models())
    created = statements(step_file(project, 1, "up", "postgres"))
    up, down = generated(project, pkg, models(declared=False))

    assert up == [drop_policy(TENANT_POLICY), no_force(), disable()]
    assert down == [enable(), force(), create_policy(created, TENANT_POLICY)]
    if db.backend == "postgres":
        assert up == reconcile(project, db)

    round_trip(project, db)
    if db.backend == "postgres":
        with tenant_role(db) as role:
            assert visible(db, role, {}) == ["ann", "bob", "cyd"]
            # The down puts the fence back.
            assert run("migrate", "down", "--yes", "--url", db.url) == 0
            assert visible(db, role, {"app.tenant": TENANT_A}) == ["ann", "bob"]


# -- a second policy on a table that already has row security ----------------------------


@backend_matrix
def test_a_second_policy_is_its_create_policy_alone_and_its_down_drops_it_alone(
    project, pkg, db
):
    tenant = TENANT.format(setting="app.tenant")
    start(project, pkg, db, models(tenant))
    up, down = generated(project, pkg, models(tenant, OWNER))

    assert len(up) == 1 and up[0].startswith(f'CREATE POLICY "{OWNER_POLICY}"')
    assert up[0] == create_policy(
        json.loads(_core._plan_row_security(json.dumps(model_ir(project, 2))))[
            "statements"
        ],
        OWNER_POLICY,
    )
    assert down == [drop_policy(OWNER_POLICY)]
    if db.backend == "postgres":
        assert up == reconcile(project, db)

    round_trip(project, db)
    if db.backend == "postgres":
        with tenant_role(db) as role:
            # Restrictive: both the tenant and the owner must match.
            assert visible(db, role, {"app.tenant": TENANT_A, "app.owner": "bob"}) == [
                "bob"
            ]


# -- SQLite-only projects ---------------------------------------------------------------


@pytest.mark.parametrize(
    "before, after",
    [
        pytest.param(models(declared=False), models(), id="e1"),
        pytest.param(models(), models(setting="app.tenant_id"), id="e2"),
        pytest.param(models(), models(declared=False), id="e3"),
    ],
)
def test_a_sqlite_only_project_has_nothing_to_write(project, pkg, before, after):
    write_config(project, pkg, '["sqlite"]')
    write_models(project, pkg, before)
    new("create")
    write_models(project, pkg, after)
    assert _new_capturing("order_rls")[0] == 0
    assert [p.name for p in (project / "migrations").iterdir() if p.is_dir()] == [
        "0001_create"
    ]
