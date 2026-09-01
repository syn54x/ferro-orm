"""Row security declaration + create-pass emission (#409, PRD #406).

A model declares ``__ferro_rls__`` once and a freshly created table comes up
with row-level security on and its ``rls_<table>_<name>`` policies in place, so
the database — not application discipline — decides which rows a query sees.

The live assertions run against the catalog (``pg_policies``, ``pg_class``)
because that is what actually enforces. The enforcement assertions run as a
created ``NOSUPERUSER`` role: superusers bypass RLS unconditionally (``FORCE``
included) and the matrix connects as one, so a green test under ``postgres``
would prove nothing.

Reconciliation of live tables — drift, orphans, the one-way flags — is
deliberately absent here: the create pass owns only missing tables (ADR-0010).
It lives in ``test_row_security_reconcile.py`` / ``_rebuild.py`` / ``_orphans.py``.
"""

import datetime
import json
import uuid
from typing import Annotated, ClassVar

import pytest

from ferro import (
    Field,
    Model,
    RowPolicy,
    RowSecurity,
    clear_registry,
    connect,
    create_tables,
    engines,
    reset_engine,
)
from ferro.rowsecurity import COMMANDS
from ferro._core import (
    _plan_row_security,
    _render_create_table_sql_for_test,
    _rls_command_matrix,
)
from ferro.ir.compiler import compile_registry_schema_ir
from ferro.raw import execute, fetch_all, fetch_one

LEDGER_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
LEDGER_B = uuid.UUID("22222222-2222-4222-8222-222222222222")

SETTING = "pinch.ledger_id"
MEMBER_SETTING = "pinch.member"

#: The exact expression the shorthand renders, for USING and WITH CHECK both.
#: The NULLIF is load-bearing: a set-then-RESET custom GUC reads back as the
#: empty string, and ``''::uuid`` is an error — without it a fail-closed policy
#: would become a hard error on every query.
SHORTHAND_EXPR = (
    "\"ledger_id\" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid"
)
POLICY_NAME = "rls_ledgerrow_ledger_id"
CREATE_POLICY_SQL = (
    f'CREATE POLICY "{POLICY_NAME}" ON "ledgerrow" FOR ALL '
    f"USING ({SHORTHAND_EXPR}) WITH CHECK ({SHORTHAND_EXPR})"
)


def _rewind_registry() -> None:
    """Drop every registered model so the same table can be redeclared."""
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


@pytest.fixture(autouse=True)
def cleanup_registry():
    _rewind_registry()
    yield
    _rewind_registry()


# ---------------------------------------------------------------------------
# Model shapes
# ---------------------------------------------------------------------------


def _define_ledger_row(*, force: bool = True) -> type[Model]:
    class LedgerRow(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        label: str

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(column="ledger_id", setting=SETTING),
            force=force,
        )

    return LedgerRow


def _define_doc() -> type[Model]:
    """Owner-full-access + invitee-read-only, AND-composed with a tenant fence.

    Three policies that only make sense together:

    * ``tenant`` is **restrictive**, so it AND-composes with everything below —
      no ledger scope, no rows, whoever you are.
    * ``owner_all`` is permissive and unscoped by command: the owner reads and
      writes their own docs.
    * ``invitee_read`` is permissive and ``command="select"``: a member of the
      doc can read it and nothing more.
    """

    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        owner: str
        title: str

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(
                name="tenant", column="ledger_id", setting=SETTING, restrictive=True
            ),
            RowPolicy(
                name="owner_all",
                using=f"\"owner\" = NULLIF(current_setting('{MEMBER_SETTING}', true), '')",
                with_check=f"\"owner\" = NULLIF(current_setting('{MEMBER_SETTING}', true), '')",
            ),
            RowPolicy(
                name="invitee_read",
                command="select",
                using=(
                    '"id" IN (SELECT doc_id FROM membership WHERE member = '
                    f"NULLIF(current_setting('{MEMBER_SETTING}', true), ''))"
                ),
            ),
        )

    return Doc


# ---------------------------------------------------------------------------
# Class-definition-time validation (no database needed)
# ---------------------------------------------------------------------------


def test_shorthand_on_an_unsupported_column_type_points_at_the_raw_form():
    with pytest.raises(TypeError) as excinfo:

        class Stamped(Model):
            id: int | None = Field(default=None, primary_key=True)
            created_at: datetime.datetime

            __ferro_rls__: ClassVar = RowSecurity(
                RowPolicy(column="created_at", setting=SETTING)
            )

    message = str(excinfo.value)
    assert "created_at" in message
    assert "timestamptz" in message
    assert "raw form" in message
    assert "uuid, text/varchar and the integer families" in message


def test_shorthand_on_an_unknown_column_names_the_valid_ones():
    with pytest.raises(TypeError) as excinfo:

        class Unknown(Model):
            id: int | None = Field(default=None, primary_key=True)
            ledger_id: uuid.UUID

            __ferro_rls__: ClassVar = RowSecurity(
                RowPolicy(column="tenant_id", setting=SETTING)
            )

    message = str(excinfo.value)
    assert "unknown column 'tenant_id'" in message
    assert "ledger_id" in message


def test_duplicate_policy_names_per_model_are_a_declaration_error():
    with pytest.raises(TypeError, match="duplicate row-policy name 'ledger_id'"):

        class Dupe(Model):
            id: int | None = Field(default=None, primary_key=True)
            ledger_id: uuid.UUID

            __ferro_rls__: ClassVar = RowSecurity(
                RowPolicy(column="ledger_id", setting=SETTING),
                RowPolicy(name="ledger_id", using="true"),
            )


def test_raw_form_requires_a_name():
    with pytest.raises(TypeError, match="requires name="):
        RowPolicy(using="true")


def test_unknown_command_is_rejected():
    with pytest.raises(TypeError, match="not a supported command"):
        RowPolicy(column="ledger_id", setting=SETTING, command="truncate")


@pytest.mark.parametrize("command", ["all", "select", "insert", "update", "delete"])
def test_every_supported_command_is_accepted(command):
    kwargs = {"with_check": "true"} if command == "insert" else {"using": "true"}
    assert RowPolicy(name="p", command=command, **kwargs).command == command


def test_the_command_allowlist_comes_from_the_shared_lowering_layer():
    """``COMMANDS`` is not a Python-side copy — it is Rust's table, read once.

    A command added in ``ferro-ddl-lowering`` therefore becomes declarable with
    no Python edit, and cannot drift out of the clause validation below.
    """
    assert COMMANDS == ("all", "select", "insert", "update", "delete")
    assert [row["command"] for row in json.loads(_rls_command_matrix())] == list(
        COMMANDS
    )


@pytest.mark.parametrize("command", ["all", "select", "insert", "update", "delete"])
def test_declaration_validation_agrees_with_what_the_renderer_emits(command):
    """The accept/reject matrix the declaration enforces is the same one the
    renderer filters clauses with — derived here from the rendered SQL, not
    restated. If the two ever diverged, a declaration ferro accepted would emit
    a clause Postgres rejects (or silently drop one the author asked for)."""

    class Scoped(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(
                name="scoped", column="ledger_id", setting=SETTING, command=command
            )
        )

    model_ir = next(
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == "scoped"
    )
    rendered = json.loads(_plan_row_security(json.dumps(model_ir)))["statements"][-1]
    emits_using = " USING (" in rendered
    emits_with_check = " WITH CHECK (" in rendered

    # What the declaration surface believes, straight from the FFI table.
    declared = {row["command"]: row for row in json.loads(_rls_command_matrix())}[
        command
    ]
    assert declared["using"] is emits_using
    assert declared["with_check"] is emits_with_check

    # And the raw form's validation follows that same table in both directions.
    if emits_using:
        assert RowPolicy(name="p", command=command, using="true").using == "true"
    else:
        with pytest.raises(TypeError, match="use with_check= instead"):
            RowPolicy(name="p", command=command, using="true", with_check="true")
    if emits_with_check:
        assert (
            RowPolicy(
                name="p",
                command=command,
                with_check="true",
                **({} if command == "insert" else {"using": "true"}),
            ).with_check
            == "true"
        )
    else:
        with pytest.raises(TypeError, match="use using= instead"):
            RowPolicy(name="p", command=command, using="true", with_check="true")


def test_shorthand_and_raw_forms_do_not_mix():
    with pytest.raises(TypeError, match="not both"):
        RowPolicy(column="ledger_id", setting=SETTING, using="true")


def test_a_policy_needs_an_expression():
    with pytest.raises(TypeError, match="needs an expression"):
        RowPolicy(name="p")


@pytest.mark.parametrize(
    "setting",
    [
        "ledger_id",  # not namespaced — a built-in GUC, not tenancy scope
        "pinch.",  # empty second segment
        ".ledger_id",  # empty namespace
        "9pinch.ledger_id",  # namespace must start like an identifier
        "pinch.ledger id",  # whitespace
        "pinch.ledger'id",  # the quote that must never reach the SQL literal
        "pinch.ledger\\id",  # nor the backslash, if standard_conforming_strings is off
    ],
)
def test_setting_keys_must_be_dotted_identifiers(setting):
    """The key is inlined into the policy body as a single-quoted literal.

    Ferro doubles quotes when it renders, but that escape is only sound while
    ``standard_conforming_strings`` is on. Constraining the key to identifier
    characters here means nothing that needs escaping can reach the literal in
    the first place; the doubling stays as defense in depth."""
    with pytest.raises(ValueError, match="not a custom Postgres setting key"):
        RowPolicy(column="ledger_id", setting=setting)


def test_distinct_policy_names_that_truncate_together_are_rejected():
    """``rls_<table>_<name>`` is cut to PostgreSQL's 63-byte identifier limit,
    so two long, different names can land on one policy name. Deduping the
    declared suffix would let that pair through and emit two CREATE POLICY
    statements with the same name."""
    long_a = "a" * 60 + "_alpha"
    long_b = "a" * 60 + "_beta"
    with pytest.raises(TypeError, match="both resolve to the live policy name"):

        class Collide(Model):
            id: int | None = Field(default=None, primary_key=True)
            ledger_id: uuid.UUID

            __ferro_rls__: ClassVar = RowSecurity(
                RowPolicy(name=long_a, column="ledger_id", setting=SETTING),
                RowPolicy(name=long_b, column="ledger_id", setting=SETTING),
            )


def test_a_shorthand_over_a_forward_fk_revalidates_on_the_resolved_pass():
    """A forward-referenced FK's shadow column only learns its real storage
    when relationships resolve. The cast check re-runs there, so a target whose
    PK turns out to be unsupported fails pointing at the declaration — and it
    stays a TypeError, the class-definition contract, not an internal error."""
    from ferro import BackRef, ForeignKey, Relation
    from ferro.relations import resolve_relationships

    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner: Annotated["Owner", ForeignKey(related_name="docs")]

        # Passes at class-body time: the shadow column is provisionally integer.
        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(column="owner_id", setting="pinch.owner")
        )

    class Owner(Model):
        id: datetime.datetime = Field(primary_key=True)
        docs: Relation[list["Doc"]] = BackRef()

    with pytest.raises(TypeError) as excinfo:
        resolve_relationships()

    message = str(excinfo.value)
    assert "Doc.__ferro_rls__" in message
    assert "owner_id" in message
    assert "timestamptz" in message


def test_using_on_a_for_insert_policy_is_rejected():
    with pytest.raises(TypeError, match="use with_check= instead"):
        RowPolicy(name="p", command="insert", using="true", with_check="true")


def test_with_check_on_a_read_only_command_is_rejected():
    with pytest.raises(TypeError, match="use using= instead"):
        RowPolicy(name="p", command="select", using="true", with_check="true")


def test_ferro_rls_must_be_a_row_security_object():
    with pytest.raises(TypeError, match="must be a RowSecurity object"):

        class Wrong(Model):
            id: int | None = Field(default=None, primary_key=True)
            ledger_id: uuid.UUID

            __ferro_rls__: ClassVar = (RowPolicy(column="ledger_id", setting=SETTING),)


# ---------------------------------------------------------------------------
# Rendering: one decision seam, two doors (I-1)
# ---------------------------------------------------------------------------


def _ledgerrow_ir() -> dict:
    return next(
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == "ledgerrow"
    )


def test_declaration_lowers_into_the_schema_ir_envelope():
    _define_ledger_row()
    assert _ledgerrow_ir()["row_security"] == {
        "force": True,
        "policies": [
            {
                "name": POLICY_NAME,
                "command": "all",
                "restrictive": False,
                "expr": {
                    "kind": "setting",
                    "column": "ledger_id",
                    "setting": SETTING,
                },
            }
        ],
    }


def test_a_model_without_a_declaration_keeps_an_unchanged_envelope():
    class Plain(Model):
        id: int | None = Field(default=None, primary_key=True)
        label: str

    model_ir = next(
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == "plain"
    )
    assert "row_security" not in model_ir


def test_row_security_statement_parity_pin():
    """The FFI the Alembic operation (#414) consumes renders the same bytes
    the create pass executes. If either side drifts, the two migration doors
    would police the same model differently."""
    _define_ledger_row()
    model_ir = _ledgerrow_ir()
    plan = json.loads(_plan_row_security(json.dumps(model_ir)))
    assert plan["names"] == [POLICY_NAME]
    assert plan["statements"] == [
        'ALTER TABLE "ledgerrow" ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE "ledgerrow" FORCE ROW LEVEL SECURITY',
        CREATE_POLICY_SQL,
    ]
    assert plan["warning"] is None

    _, post_create, _ = _render_create_table_sql_for_test(
        "ledgerrow",
        json.dumps({"dialect_agnostic": True, "models": [model_ir]}),
        "postgres",
    )
    assert post_create[-3:] == plan["statements"]


def test_force_false_drops_only_the_force_statement():
    _define_ledger_row(force=False)
    plan = json.loads(_plan_row_security(json.dumps(_ledgerrow_ir())))
    assert plan["statements"] == [
        'ALTER TABLE "ledgerrow" ENABLE ROW LEVEL SECURITY',
        CREATE_POLICY_SQL,
    ]


def test_text_columns_render_without_a_cast():
    class Tenanted(Model):
        id: int | None = Field(default=None, primary_key=True)
        tenant: str

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(column="tenant", setting="pinch.tenant")
        )

    model_ir = next(
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == "tenanted"
    )
    plan = json.loads(_plan_row_security(json.dumps(model_ir)))
    assert plan["statements"][-1] == (
        'CREATE POLICY "rls_tenanted_tenant" ON "tenanted" FOR ALL '
        "USING (\"tenant\" = NULLIF(current_setting('pinch.tenant', true), '')) "
        "WITH CHECK (\"tenant\" = NULLIF(current_setting('pinch.tenant', true), ''))"
    )


# ---------------------------------------------------------------------------
# Live create pass
# ---------------------------------------------------------------------------


async def _pg_row_security_flags(table: str) -> dict:
    row = await fetch_one(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        f"WHERE oid = '\"{table}\"'::regclass"
    )
    assert row is not None
    return row


async def _pg_policies(table: str) -> list[dict]:
    return await fetch_all(
        "SELECT policyname, permissive, cmd, qual, with_check FROM pg_policies "
        "WHERE schemaname = current_schema() AND tablename = $1 ORDER BY policyname",
        table,
    )


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_fresh_auto_migrate_creates_the_table_with_its_flags_and_policy(db_url):
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        flags = await _pg_row_security_flags("ledgerrow")
        assert flags["relrowsecurity"] is True
        assert flags["relforcerowsecurity"] is True

        policies = await _pg_policies("ledgerrow")
        assert [p["policyname"] for p in policies] == [POLICY_NAME]
        policy = policies[0]
        assert policy["permissive"] == "PERMISSIVE"
        assert policy["cmd"] == "ALL"
        # The catalog re-renders the expression; what must survive is the
        # NULLIF fail-closed shape and the column's own cast.
        for clause in (policy["qual"], policy["with_check"]):
            assert "NULLIF" in clause
            assert "current_setting('pinch.ledger_id'" in clause
            assert "::uuid" in clause


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_force_false_enables_without_forcing(db_url):
    _define_ledger_row(force=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        flags = await _pg_row_security_flags("ledgerrow")
        assert flags["relrowsecurity"] is True
        assert flags["relforcerowsecurity"] is False


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_create_pass_leaves_an_existing_table_untouched_but_warns(
    db_url, recwarn
):
    """ADR-0010: the create pass owns only missing tables, so adding a
    declaration to a live table emits nothing — but it must NOT do so quietly.

    The author now believes their rows are fenced. ``migrate_updates`` applies
    the declaration (#413) and this warning stays quiet there; on a connect that
    does NOT reconcile, it is the only thing standing between that belief and a
    silent data leak, so it fires every time and names the table."""

    class LedgerRow(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        label: str

    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row()
    recwarn.clear()
    await connect(db_url, auto_migrate=True)

    unenforced = [w for w in recwarn if "__ferro_rls__" in str(w.message)]
    assert len(unenforced) == 1, [str(w.message) for w in recwarn]
    message = str(unenforced[0].message)
    assert "ledgerrow" in message
    assert "NOT filtered" in message
    assert "ADR-0010" in message

    async with engines.session():
        flags = await _pg_row_security_flags("ledgerrow")
        assert flags["relrowsecurity"] is False
        assert await _pg_policies("ledgerrow") == []

    # And again on the NEXT connect — the gap does not go quiet after one
    # boot, which is the whole point of warning rather than logging once.
    reset_engine()
    recwarn.clear()
    await connect(db_url, auto_migrate=True)
    assert len([w for w in recwarn if "__ferro_rls__" in str(w.message)]) == 1


@pytest.mark.backend_matrix
@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_sqlite_registers_warns_and_skips_the_ddl(db_url, recwarn):
    LedgerRow = _define_ledger_row()
    await connect(db_url, auto_migrate=True)

    warnings = [w for w in recwarn if "row-level security" in str(w.message)]
    assert len(warnings) == 1, [str(w.message) for w in recwarn]
    assert "ledgerrow" in str(warnings[0].message)
    assert "PostgreSQL-only" in str(warnings[0].message)

    # The model still registers and the table is still usable.
    async with engines.session():
        await LedgerRow.create(ledger_id=LEDGER_A, label="a")
        assert len(await LedgerRow.all()) == 1
        sql = await fetch_all("SELECT sql FROM sqlite_master WHERE name = 'ledgerrow'")
        assert "POLICY" not in (sql[0]["sql"] or "").upper()


# ---------------------------------------------------------------------------
# Enforcement, as a NOSUPERUSER role
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_role():
    """A cluster-unique NOSUPERUSER role name, dropped after the test."""
    return f"ferro_rls_{uuid.uuid4().hex[:12]}"


async def _grant(role: str, table: str, *, own_table: bool = False) -> None:
    """Create the NOSUPERUSER role and give it just enough to run the queries.

    Always call this INSIDE the ``try`` whose ``finally`` drops the role: the
    role exists from the first statement on, so a later GRANT that fails would
    otherwise leak it into the cluster and break every subsequent run (roles are
    cluster-global; the per-test schema is not).
    """
    schema = (await fetch_one("SELECT current_schema() AS s"))["s"]
    await execute(f'CREATE ROLE "{role}" NOSUPERUSER')
    await execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
    await execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO "{role}"')
    await execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{role}"')
    if own_table:
        await execute(f'ALTER TABLE "{table}" OWNER TO "{role}"')


async def _drop_tenant_role(role: str, *, owned_tables: tuple[str, ...] = ()) -> None:
    """Drop the role, handing back anything it was made to own first.

    ``owned_tables`` is explicit rather than leaning on ``DROP OWNED BY`` to
    take the table with it: a test that transfers ownership should undo exactly
    that transfer, so the teardown says what it does and a future reader is not
    relying on a table disappearing as a side effect.
    """
    current = (await fetch_one("SELECT current_user AS u"))["u"]
    for table in owned_tables:
        await execute(f'ALTER TABLE "{table}" OWNER TO "{current}"')
    await execute(f'DROP OWNED BY "{role}"')
    await execute(f'DROP ROLE IF EXISTS "{role}"')


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_policy_filters_rows_for_a_non_superuser_role(db_url, tenant_role):
    from ferro import transaction

    LedgerRow = _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        # Seeded as the superuser the matrix connects as — RLS does not apply.
        await LedgerRow.create(ledger_id=LEDGER_A, label="a1")
        await LedgerRow.create(ledger_id=LEDGER_A, label="a2")
        await LedgerRow.create(ledger_id=LEDGER_B, label="b1")

        try:
            await _grant(tenant_role, "ledgerrow")
            # No setting at all: fail closed, and NOT an error.
            async with transaction() as tx:
                await tx.execute(f'SET LOCAL ROLE "{tenant_role}"')
                assert await tx.fetch_all("SELECT label FROM ledgerrow") == []

            # Scoped: only that ledger's rows, through the ORM.
            async with transaction() as tx:
                await tx.execute(f'SET LOCAL ROLE "{tenant_role}"')
                await tx.execute(
                    "SELECT set_config($1, $2, true)", SETTING, str(LEDGER_A)
                )
                rows = await LedgerRow.all()
                assert sorted(row.label for row in rows) == ["a1", "a2"]

            # Set, then RESET: the GUC reads back as '', and the NULLIF turns
            # that into zero rows instead of a `''::uuid` cast error.
            async with transaction() as tx:
                await tx.execute(f'SET LOCAL ROLE "{tenant_role}"')
                await tx.execute(
                    "SELECT set_config($1, $2, true)", SETTING, str(LEDGER_A)
                )
                assert len(await tx.fetch_all("SELECT label FROM ledgerrow")) == 2
                await tx.execute(f"RESET {SETTING}")
                reset = await tx.fetch_one(
                    "SELECT current_setting($1, true) AS v", SETTING
                )
                assert reset["v"] == ""
                assert await tx.fetch_all("SELECT label FROM ledgerrow") == []
        finally:
            await _drop_tenant_role(tenant_role)


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_with_check_rejects_a_cross_tenant_insert(db_url, tenant_role):
    from ferro import transaction

    LedgerRow = _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        try:
            await _grant(tenant_role, "ledgerrow")
            async with transaction() as tx:
                await tx.execute(f'SET LOCAL ROLE "{tenant_role}"')
                await tx.execute(
                    "SELECT set_config($1, $2, true)", SETTING, str(LEDGER_A)
                )
                # In scope: accepted.
                await LedgerRow.create(ledger_id=LEDGER_A, label="mine")
                # Out of scope: the WITH CHECK half of the same declaration.
                with pytest.raises(Exception) as excinfo:
                    await LedgerRow.create(ledger_id=LEDGER_B, label="theirs")
                assert "policy" in str(excinfo.value).lower()
        finally:
            await _drop_tenant_role(tenant_role)


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_force_binds_the_table_owner(db_url, tenant_role):
    """Without FORCE, a table's owner is exempt from its own policies — which
    is every single-role deployment. With it, the owner is filtered too."""
    from ferro import transaction

    LedgerRow = _define_ledger_row(force=True)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await LedgerRow.create(ledger_id=LEDGER_A, label="a1")
        try:
            await _grant(tenant_role, "ledgerrow", own_table=True)
            async with transaction() as tx:
                await tx.execute(f'SET LOCAL ROLE "{tenant_role}"')
                owner = await tx.fetch_one(
                    "SELECT tableowner FROM pg_tables WHERE tablename = 'ledgerrow'"
                )
                assert owner["tableowner"] == tenant_role
                assert await tx.fetch_all("SELECT label FROM ledgerrow") == []
        finally:
            # This test moved ownership, so this test moves it back —
            # rather than leaning on DROP OWNED BY to take the table with
            # it as a side effect.
            await _drop_tenant_role(tenant_role, owned_tables=("ledgerrow",))


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_command_scoped_and_restrictive_policies_compose(db_url, tenant_role):
    from ferro import transaction

    Doc = _define_doc()
    await connect(db_url)
    async with engines.session():
        # The invitee policy reads a table ferro does not own; it must exist
        # before CREATE POLICY references it.
        await execute("CREATE TABLE membership (doc_id INTEGER, member TEXT)")
        await create_tables()

        doc = await Doc.create(ledger_id=LEDGER_A, owner="alice", title="plan")
        await execute(
            "INSERT INTO membership (doc_id, member) VALUES ($1, $2)", doc.id, "bob"
        )

        try:
            await _grant(tenant_role, "doc")
            await execute(f'GRANT SELECT ON membership TO "{tenant_role}"')
            # Owner in scope: reads and writes.
            async with transaction() as tx:
                await tx.execute(f'SET LOCAL ROLE "{tenant_role}"')
                await tx.execute(
                    "SELECT set_config($1, $2, true)", SETTING, str(LEDGER_A)
                )
                await tx.execute(
                    "SELECT set_config($1, $2, true)", MEMBER_SETTING, "alice"
                )
                assert len(await tx.fetch_all("SELECT id FROM doc")) == 1
                await tx.execute("UPDATE doc SET title = 'renamed'")
                assert (await tx.fetch_one("SELECT title FROM doc"))[
                    "title"
                ] == "renamed"

            # Invitee: SELECT only — no permissive policy covers UPDATE, so the
            # row is invisible to it and the update touches nothing.
            async with transaction() as tx:
                await tx.execute(f'SET LOCAL ROLE "{tenant_role}"')
                await tx.execute(
                    "SELECT set_config($1, $2, true)", SETTING, str(LEDGER_A)
                )
                await tx.execute(
                    "SELECT set_config($1, $2, true)", MEMBER_SETTING, "bob"
                )
                assert len(await tx.fetch_all("SELECT id FROM doc")) == 1
                await tx.execute("UPDATE doc SET title = 'hijacked'")
            assert (await fetch_one("SELECT title FROM doc"))["title"] == "renamed"

            # Wrong ledger: the RESTRICTIVE policy AND-composes, so even the
            # owner's permissive policy cannot bring the row back.
            async with transaction() as tx:
                await tx.execute(f'SET LOCAL ROLE "{tenant_role}"')
                await tx.execute(
                    "SELECT set_config($1, $2, true)", SETTING, str(LEDGER_B)
                )
                await tx.execute(
                    "SELECT set_config($1, $2, true)", MEMBER_SETTING, "alice"
                )
                assert await tx.fetch_all("SELECT id FROM doc") == []
        finally:
            await _drop_tenant_role(tenant_role)


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_declared_policies_land_in_the_catalog_as_declared(db_url):
    _define_doc()
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TABLE membership (doc_id INTEGER, member TEXT)")
        await create_tables()

        policies = {p["policyname"]: p for p in await _pg_policies("doc")}
        assert set(policies) == {
            "rls_doc_tenant",
            "rls_doc_owner_all",
            "rls_doc_invitee_read",
        }
        assert policies["rls_doc_tenant"]["permissive"] == "RESTRICTIVE"
        assert policies["rls_doc_owner_all"]["permissive"] == "PERMISSIVE"
        assert policies["rls_doc_invitee_read"]["cmd"] == "SELECT"
        # A FOR SELECT policy takes USING alone — Postgres rejects WITH CHECK
        # there, so the renderer must not offer one.
        assert policies["rls_doc_invitee_read"]["with_check"] is None
        assert policies["rls_doc_owner_all"]["with_check"] is not None


# ---------------------------------------------------------------------------
# A half-created policed table would be unrecoverable
# ---------------------------------------------------------------------------


def _define_broken_policy_model() -> type[Model]:
    """A raw policy naming a table that does not exist — CREATE POLICY fails.

    The realistic shape of this is not a typo: it is a raw ``using=`` reading
    another ferro model's table that sorts later in FK-dependency order, so it
    genuinely is not there yet when this table's policies go on.
    """

    class Fragile(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(column="ledger_id", setting=SETTING),
            RowPolicy(
                name="broken",
                command="select",
                using='"id" IN (SELECT doc_id FROM table_that_does_not_exist)',
            ),
        )

    return Fragile


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_failed_policy_leaves_no_table_rather_than_a_locked_out_one(db_url):
    """Creating a table is atomic, and for a policed table that is the whole
    contract — not a nicety.

    Flags land before policies, so a ``CREATE POLICY`` that fails partway would
    otherwise leave a live table with row security ENABLED and FORCED and
    **zero** policies: default-deny for every role including the owner. The
    create pass could never repair it either, because it only ever touches
    missing tables (ADR-0010). So the whole table rolls back instead.
    """
    _define_broken_policy_model()
    await connect(db_url)

    with pytest.raises(Exception) as excinfo:
        await create_tables()
    # The error names the statement, not just "connect failed".
    message = str(excinfo.value)
    assert "fragile" in message
    assert "table_that_does_not_exist" in message

    async with engines.session():
        rows = await fetch_all(
            "SELECT 1 AS present FROM pg_class "
            "WHERE relname = 'fragile' AND relnamespace = current_schema()::regnamespace"
        )
        assert rows == [], "a failed policy must leave NO table behind"

    # And the fix is just to fix the declaration: nothing needs cleaning up.
    _rewind_registry()
    LedgerRow = _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        flags = await _pg_row_security_flags("ledgerrow")
        assert flags["relrowsecurity"] is True
        assert [p["policyname"] for p in await _pg_policies("ledgerrow")] == [
            POLICY_NAME
        ]
        await LedgerRow.create(ledger_id=LEDGER_A, label="fine")
