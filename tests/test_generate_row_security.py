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
import io
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
    empty_snapshot,
    keys,
    migration_dir,
    plan_against,
    snapshot,
)
from tests.test_migrate_new import (  # noqa: F401 - fixtures
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
from tests.test_row_security_create_pass import created_row_security

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


#: Two more models, neither referencing the other by a foreign key:
#: ``document`` is created first, and its policy reads ``membership`` (#753).
READS_ANOTHER_TABLE = """

class Document(Model):
    __ferro_rls__ = RowSecurity(
        RowPolicy(
            name="members",
            using='"owner_id" IN (SELECT user_id FROM membership)',
        )
    )
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    owner_id: int


class Membership(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    user_id: int
"""


def reads_a_column(*, named: bool, document: bool | None) -> str:
    """``RlsTeam``, with its ``name`` column when ``named``, and (unless
    ``document`` is ``None``) ``RlsDocument``, under a policy that reads
    ``rlsteam.name`` when ``document`` is true: the models a policy reads a
    column of another table through (#995). ``RlsDocument`` sorts first, so
    its table is visited before the one whose column its policy reads."""
    policy = """    __ferro_rls__ = RowSecurity(
        RowPolicy(
            name="members",
            using='"owner_id" IN (SELECT id FROM rlsteam WHERE name IS NOT NULL)',
        )
    )
"""
    body = ""
    if document is not None:
        body += f"""

class RlsDocument(Model):
{policy if document else ""}    id: Annotated[int | None, FerroField(primary_key=True)] = None
    owner_id: int
"""
    body += """

class RlsTeam(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
"""
    if named:
        body += "    name: str | None = None\n"
    return body


#: Before and after of the three shapes of #995, by casebook case.
READS_A_CHANGED_COLUMN = {
    "E5-new-table-whose-policy-reads-an-added-column": (
        reads_a_column(named=False, document=None),
        reads_a_column(named=True, document=True),
    ),
    "E6-add-a-policy-that-reads-an-added-column": (
        reads_a_column(named=False, document=False),
        reads_a_column(named=True, document=True),
    ),
    "E7-drop-a-column-and-the-policy-that-read-it": (
        reads_a_column(named=True, document=True),
        reads_a_column(named=False, document=False),
    ),
}

ADD_NAME = 'ALTER TABLE "rlsteam" ADD COLUMN "name" varchar'
DROP_NAME = 'ALTER TABLE "rlsteam" DROP COLUMN "name"'
MEMBERS = [
    'ALTER TABLE "rlsdocument" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "rlsdocument" FORCE ROW LEVEL SECURITY',
    'CREATE POLICY "rls_rlsdocument_members" ON "rlsdocument" FOR ALL '
    'USING ("owner_id" IN (SELECT id FROM rlsteam WHERE name IS NOT NULL))',
]
NO_MEMBERS = [
    'DROP POLICY "rls_rlsdocument_members" ON "rlsdocument"',
    DROP_NAME,
    'ALTER TABLE "rlsdocument" NO FORCE ROW LEVEL SECURITY',
    'ALTER TABLE "rlsdocument" DISABLE ROW LEVEL SECURITY',
]


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


def created(project: Path, number: int) -> list[str]:
    """The create pass's row-security statements for snapshot ``number``'s
    table: the one planner's flag and policy ops for the new table, rendered."""
    statements, _ = created_row_security(snapshot(project, number), TABLE)
    return statements


def reconcile(project: Path, db) -> list[str]:
    """What the pass executes for ``0002``'s declaration over the live table,
    ``migrate_destructive`` (the generator plans every drop): the one
    planner's rendered plan from the live schema and its facts."""

    async def read() -> list[str]:
        name = f"rls_live_{uuid.uuid4().hex}"
        declared = json.dumps(snapshot(project, 2))
        await ferro.connect(db.url, name=name)
        try:
            live, facts = await _core._live_schema_ir(name, declared)
        finally:
            await _core._disconnect(name)
        plan = json.loads(
            _core._plan_from_ir(
                live,
                declared,
                "postgres",
                '{"destructive": true}',
                True,
                facts,
            )
        )
        return [sql for op in plan["operations"] for sql in op["statements"]]

    return asyncio.run(read())


@contextlib.contextmanager
def tenant_role(db, pg_role) -> Iterator[str]:
    """A NOSUPERUSER, non-BYPASSRLS login role that may read the table,
    named by ``pg_role`` so it is dropped with the schema whatever happens."""
    role = pg_role("rlsgen")
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
    project, pkg, db, pg_role
):
    start(project, pkg, db, models(declared=False))
    up, down = generated(project, pkg, models())

    # The create pass's statements for the declaration, byte for byte.
    assert up == created(project, 2)
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
        with tenant_role(db, pg_role) as role:
            assert visible(db, role, {"app.tenant": TENANT_A}) == ["ann", "bob"]
            assert visible(db, role, {"app.tenant": TENANT_B}) == ["cyd"]
            assert visible(db, role, {}) == []
            # Reverted, the table filters nothing again.
            assert run("migrate", "down", "--yes", "--url", db.url) == 0
            assert visible(db, role, {}) == ["ann", "bob", "cyd"]


# -- B1: a new table with row security ------------------------------------------------


@backend_matrix
def test_b1_a_new_table_creates_its_row_security_after_the_table_and_drops_its_policy_first(
    project, pkg, db, pg_role
):
    write_config(project, pkg)
    write_models(project, pkg, models())
    new("create")
    up = statements(step_file(project, 1, "up", "postgres"))
    at = next(i for i, s in enumerate(up) if s.startswith("CREATE TABLE"))
    assert up[at + 1 :] == created(project, 1)
    # The policy goes ahead of the table; the flags go with the table.
    assert statements(step_file(project, 1, "down", "postgres")) == [
        drop_policy(TENANT_POLICY),
        f'DROP TABLE "{TABLE}"',
    ]
    assert statements(step_file(project, 1, "down", "sqlite")) == [
        f'DROP TABLE "{TABLE}"'
    ]
    assert "POLICY" not in step_file(project, 1, "up", "sqlite").read_text()

    assert run("migrate", "up", "--url", db.url) == 0
    assert plan_against(db, snapshot(project, 1), empty_snapshot(project)) == []
    if db.backend == "postgres":
        db.execute(
            f'INSERT INTO "{TABLE}" ("tenant_id", "owner") VALUES '
            f"('{TENANT_A}', 'ann'), ('{TENANT_B}', 'cyd')"
        )
        with tenant_role(db, pg_role) as role:
            assert visible(db, role, {"app.tenant": TENANT_B}) == ["cyd"]
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert keys(db) == []
    assert TABLE not in db.tables()
    assert plan_against(db, empty_snapshot(project), snapshot(project, 1)) == []


@backend_matrix
def test_b1_a_policy_may_read_a_table_the_same_migration_creates(project, pkg, db):
    """Every new table's row security runs after the last ``CREATE TABLE`` of
    the step, and its policies are dropped before the first ``DROP TABLE``
    of the down: Postgres refuses a policy over a table that does not exist
    yet, and the drop of a table a policy still reads (#753)."""
    write_config(project, pkg)
    write_models(project, pkg, READS_ANOTHER_TABLE)
    new("create")
    up = statements(step_file(project, 1, "up", "postgres"))
    assert [s.split(" (")[0] for s in up[:2]] == [
        'CREATE TABLE IF NOT EXISTS "document"',
        'CREATE TABLE IF NOT EXISTS "membership"',
    ]
    assert up[2:] == [
        'ALTER TABLE "document" ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE "document" FORCE ROW LEVEL SECURITY',
        'CREATE POLICY "rls_document_members" ON "document" FOR ALL '
        'USING ("owner_id" IN (SELECT user_id FROM membership))',
    ]
    assert statements(step_file(project, 1, "down", "postgres")) == [
        'DROP POLICY "rls_document_members" ON "document"',
        'DROP TABLE "membership"',
        'DROP TABLE "document"',
    ]

    assert run("migrate", "up", "--url", db.url) == 0
    assert plan_against(db, snapshot(project, 1), empty_snapshot(project)) == []
    if db.backend == "postgres":
        assert db.rows(
            "SELECT policyname FROM pg_policies "
            f"WHERE schemaname = '{db.schema}' AND tablename = 'document'"
        ) == [("rls_document_members",)]
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert not {"document", "membership"} & db.tables()
    assert plan_against(db, empty_snapshot(project), snapshot(project, 1)) == []


def schema_steps(capsys, db) -> list[list[str]]:
    """Each ``01_schema`` line of ``ferro migrate status --steps``, split."""
    capsys.readouterr()
    assert run("migrate", "status", "--steps", "--url", db.url) == 0
    out = capsys.readouterr().out
    return [line.split() for line in out.splitlines() if "01_schema" in line]


@backend_matrix
def test_a_step_that_only_creates_a_table_with_row_security_is_expand(
    project, pkg, db, capsys
):
    """The row security placed after the last ``CREATE TABLE`` is part of
    creating the table: the previous release never writes it (#1001)."""
    write_config(project, pkg)
    write_models(project, pkg, models())
    new("create")
    assert run("migrate", "up", "--url", db.url) == 0
    assert schema_steps(capsys, db) == [
        [f"01_schema.up.{db.backend}.sql", "applied", "expand"]
    ]


@backend_matrix
@pytest.mark.parametrize("case", sorted(READS_A_CHANGED_COLUMN))
def test_a_policy_may_read_a_column_the_same_migration_adds_or_drops(
    project, pkg, db, case
):
    """Every policy drop of a migration goes before its first column change
    and every policy create after its last, both ways: Postgres refuses a
    policy over a column that does not exist yet, and the drop of a column a
    policy still reads (#995)."""
    before, after = READS_A_CHANGED_COLUMN[case]
    write_config(project, pkg)
    write_models(project, pkg, before)
    new("create")
    write_models(project, pkg, after)
    new("edit")
    up = statements(step_file(project, 2, "up", "postgres"))
    down = statements(step_file(project, 2, "down", "postgres"))
    drop_table = ['DROP TABLE "rlsdocument"']
    if case.startswith("E5"):
        assert up[0].startswith('CREATE TABLE IF NOT EXISTS "rlsdocument" (')
        assert up[1:] == [ADD_NAME, *MEMBERS]
        assert down == [NO_MEMBERS[0], DROP_NAME, *drop_table]
    elif case.startswith("E6"):
        assert up == [ADD_NAME, *MEMBERS]
        assert down == NO_MEMBERS
    else:
        assert up == NO_MEMBERS
        assert down == [ADD_NAME, *MEMBERS]

    assert run("migrate", "up", "--url", db.url) == 0
    assert run("migrate", "drift", "--url", db.url) == 0
    assert plan_against(db, snapshot(project, 2), snapshot(project, 1)) == []
    if db.backend == "postgres":
        assert db.rows(
            "SELECT policyname FROM pg_policies "
            f"WHERE schemaname = '{db.schema}' AND tablename = 'rlsdocument'"
        ) == ([] if case.startswith("E7") else [("rls_rlsdocument_members",)])
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert plan_against(db, snapshot(project, 1), snapshot(project, 2)) == []
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert not {"rlsdocument", "rlsteam"} & db.tables()


MEMBERSHIP_ONLY = READS_ANOTHER_TABLE[READS_ANOTHER_TABLE.index("class Membership") :]


@backend_matrix
def test_a_new_table_whose_policy_reads_an_existing_table_is_expand(
    project, pkg, db, capsys
):
    """A policy refuses rows of its own table only: the previous release
    writes ``membership`` and never ``document``, so ``0002`` is expand and
    an insert into ``membership`` still succeeds beside it (#1001)."""
    write_config(project, pkg)
    write_models(project, pkg, MEMBERSHIP_ONLY)
    new("membership")
    write_models(project, pkg, READS_ANOTHER_TABLE)
    new("document")
    assert run("migrate", "up", "--url", db.url) == 0
    db.execute('INSERT INTO "membership" ("user_id") VALUES (7)')
    assert db.rows('SELECT "user_id" FROM "membership"') == [(7,)]
    assert schema_steps(capsys, db)[1][-2:] == ["applied", "expand"]


# -- E2: a policy body changed ---------------------------------------------------------


@backend_matrix
def test_e2_a_changed_body_is_dropped_and_recreated_and_its_down_restores_the_old_one(
    project, pkg, db, pg_role
):
    start(project, pkg, db, models())
    old_create = create_policy(
        statements(step_file(project, 1, "up", "postgres")), TENANT_POLICY
    )
    up, down = generated(project, pkg, models(setting="app.tenant_id"))

    new_create = created(project, 2)[-1]
    assert "current_setting('app.tenant_id', true)" in new_create
    assert up == [drop_policy(TENANT_POLICY), new_create]
    assert down == [drop_policy(TENANT_POLICY), old_create]
    if db.backend == "postgres":
        assert up == reconcile(project, db)

    round_trip(project, db)
    if db.backend == "postgres":
        with tenant_role(db, pg_role) as role:
            assert visible(db, role, {"app.tenant_id": TENANT_B}) == ["cyd"]
            assert visible(db, role, {"app.tenant": TENANT_B}) == []


# -- E3: row security removed -----------------------------------------------------------


@backend_matrix
def test_e3_removed_row_security_is_dropped_and_torn_down_and_its_down_recreates_it(
    project, pkg, db, pg_role
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
        with tenant_role(db, pg_role) as role:
            assert visible(db, role, {}) == ["ann", "bob", "cyd"]
            # The down puts the fence back.
            assert run("migrate", "down", "--yes", "--url", db.url) == 0
            assert visible(db, role, {"app.tenant": TENANT_A}) == ["ann", "bob"]


# -- a second policy on a table that already has row security ----------------------------


@backend_matrix
def test_a_second_policy_is_its_create_policy_alone_and_its_down_drops_it_alone(
    project, pkg, db, pg_role
):
    tenant = TENANT.format(setting="app.tenant")
    start(project, pkg, db, models(tenant))
    up, down = generated(project, pkg, models(tenant, OWNER))

    assert len(up) == 1 and up[0].startswith(f'CREATE POLICY "{OWNER_POLICY}"')
    assert up[0] == create_policy(
        created(project, 2),
        OWNER_POLICY,
    )
    assert down == [drop_policy(OWNER_POLICY)]
    if db.backend == "postgres":
        assert up == reconcile(project, db)

    round_trip(project, db)
    if db.backend == "postgres":
        with tenant_role(db, pg_role) as role:
            # Restrictive: both the tenant and the owner must match.
            assert visible(db, role, {"app.tenant": TENANT_A, "app.owner": "bob"}) == [
                "bob"
            ]


# -- a raw policy body edited ------------------------------------------------------------


def raw(owner: str) -> str:
    return f'RowPolicy(name="raw", command="select", using="\\"owner\\" = \'{owner}\'")'


@backend_matrix
def test_an_edited_raw_body_is_rebuilt_and_its_down_restores_the_parents_body(
    project, pkg, db, pg_role
):
    start(project, pkg, db, models(raw("ann")))
    old_create = create_policy(
        statements(step_file(project, 1, "up", "postgres")), "rls_rlsorder_raw"
    )
    write_models(project, pkg, models(raw("bob")))
    code, out, err = _new_capturing_all("raw_owner")
    assert code == 0, err
    # Unverifiable is a live-only category (ADR-0019): no such report here.
    assert "no longer matches" not in err
    up = statements(step_file(project, 2, "up", "postgres"))
    down = statements(step_file(project, 2, "down", "postgres"))
    new_create = created(project, 2)[-1]
    assert "'bob'" in new_create
    assert up == [drop_policy("rls_rlsorder_raw"), new_create]
    assert down == [drop_policy("rls_rlsorder_raw"), old_create]
    for direction in ("up", "down"):
        assert step_file(project, 2, direction, "sqlite").read_text() == NOT_APPLICABLE

    round_trip(project, db)
    if db.backend == "postgres":
        with tenant_role(db, pg_role) as role:
            assert visible(db, role, {}) == ["bob"]
            assert run("migrate", "down", "--yes", "--url", db.url) == 0
            assert visible(db, role, {}) == ["ann"]


def _new_capturing_all(name: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run("migrate", "new", name)
    return code, out.getvalue(), err.getvalue()


# -- the old side is the caller's word -------------------------------------------------


def test_the_planner_takes_the_old_side_from_the_caller_never_from_missing_facts(
    project, pkg
):
    """Over FFI, no ``facts_json`` means two declared snapshots: the parent is
    the proof ferro set the flags, so a dropped policy-less declaration is
    torn down. Any ``facts_json`` is a live database's, which must cover each
    of its tables; with the table's facts the pass's posture stands."""
    write_config(project, pkg, '["postgres"]')
    write_models(project, pkg, models())
    new("create")
    parent = snapshot(project, 1)
    bare = json.loads(json.dumps(parent))
    bare["payload"]["models"][0]["row_security"] = {"force": True, "policies": []}
    gone = json.loads(json.dumps(parent))
    gone["payload"]["models"][0]["row_security"] = None

    def kinds(facts_json: str | None) -> list[str]:
        plan = _core._plan_from_ir(
            json.dumps(bare),
            json.dumps(gone),
            "postgres",
            '{"destructive": true}',
            facts_json=facts_json,
        )
        return [op["kind"] for op in json.loads(plan)["operations"]]

    assert kinds(None) == ["NoForceRowSecurity", "DisableRowSecurity"]
    live = {
        "tables": {
            TABLE: {"row_security": {"enabled": True, "forced": True, "policies": []}}
        }
    }
    assert kinds(json.dumps(live)) == []
    with pytest.raises(ValueError, match=f"no entry for table '{TABLE}'"):
        kinds("{}")


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
