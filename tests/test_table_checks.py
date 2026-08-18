"""Table checks: declaration, compilation, and inline CREATE TABLE emission.

A table check is declared as ``Check(suffix, predicate)`` in
``__ferro_checks__`` (ADR-0012). The predicate is a ferro lambda; this release
compiles the Transfer-shaped dialect — ``== None`` / ``!= None`` combined with
``&`` / ``|`` / ``~`` (ADR-0016) — and rejects everything richer at class
definition until #346 grows the dialect.

Emission is INLINE in ``CREATE TABLE`` on both dialects (ADR-0014), never a
follow-up ALTER, and the Alembic bridge carries the same named CHECKs so
autogenerate against a Rust-bootstrapped database stays empty (I-1).
"""

import json
from enum import StrEnum
from typing import Annotated, ClassVar

import pytest
import sqlalchemy as sa

from ferro import (
    BackRef,
    Check,
    CheckViolationError,
    Field,
    FerroField,
    ForeignKey,
    Model,
    Relation,
    clear_registry,
    connect,
    engines,
    ensure_resolved_modelset,
    reset_engine,
)
from ferro._core import _render_create_table_sql_for_test, _render_table_check_body
from ferro.ir.compiler import compile_registry_schema_ir
from ferro.migrations import get_metadata

OUTFLOW_CHECK_BODY = '("outflow_transaction_id" IS NULL) OR ("outflow_activity_id" IS NULL)'
OUTFLOW_CHECK_NAME = "ck_transfer_at_most_one_outflow"


@pytest.fixture(autouse=True)
def cleanup_registry():
    from ferro.registry import REGISTRY

    def _wipe() -> None:
        reset_engine()
        clear_registry()
        REGISTRY.reset_for_test()

    _wipe()
    yield
    _wipe()


def _model_payload(table_name: str) -> dict:
    """The compiled SchemaIR payload for one table (models key on identity)."""
    envelope = compile_registry_schema_ir()
    for model in envelope["payload"]["models"]:
        if model["table_name"] == table_name:
            return model
    raise AssertionError(f"{table_name} missing from the compiled modelset")


# ---------------------------------------------------------------------------
# Declaration-time failures
# ---------------------------------------------------------------------------


def test_check_rejects_a_full_constraint_name():
    """The slot is a SUFFIX; ferro owns the ``ck_<table>_`` prefix."""
    with pytest.raises(TypeError, match="suffix"):
        Check("ck_transfer_at_most_one_outflow", lambda transfer: transfer.memo == None)  # noqa: E711


@pytest.mark.parametrize("suffix", ["At_Most_One", "1st_rule", "", "has space", "trailing-"])
def test_check_rejects_suffixes_outside_the_identifier_shape(suffix):
    with pytest.raises(TypeError, match=r"\[a-z\]\[a-z0-9_\]\*"):
        Check(suffix, lambda transfer: transfer.memo == None)  # noqa: E711


def test_check_rejects_a_non_callable_predicate():
    with pytest.raises(TypeError, match="predicate"):
        Check("at_most_one", '"a" IS NULL')  # ty: ignore[invalid-argument-type]


def test_duplicate_suffixes_fail_at_class_definition():
    with pytest.raises(TypeError, match="duplicate table-check suffix 'only_one'"):

        class DupCheck(Model):
            __ferro_checks__: ClassVar[tuple[Check, ...]] = (
                Check("only_one", lambda dup: dup.left == None),  # noqa: E711
                Check("only_one", lambda dup: dup.right == None),  # noqa: E711
            )

            id: int | None = Field(default=None, primary_key=True)
            left: str | None = None
            right: str | None = None


def test_unknown_column_fails_at_class_definition():
    with pytest.raises(TypeError, match="nonexistent"):

        class BadColumnCheck(Model):
            __ferro_checks__: ClassVar[tuple[Check, ...]] = (
                Check("only_one", lambda bad: bad.nonexistent == None),  # noqa: E711
            )

            id: int | None = Field(default=None, primary_key=True)
            left: str | None = None


def test_unknown_column_suggests_a_close_match():
    with pytest.raises(TypeError, match="Did you mean 'amount'"):

        class TypoCheck(Model):
            __ferro_checks__: ClassVar[tuple[Check, ...]] = (
                Check("positive", lambda typo: typo.amont >= 0),
            )

            id: int | None = Field(default=None, primary_key=True)
            amount: int = 0


def test_non_check_entries_fail_at_class_definition():
    with pytest.raises(TypeError, match="must be a Check object"):

        class RawTupleCheck(Model):
            __ferro_checks__: ClassVar[tuple[Check, ...]] = (  # ty: ignore[invalid-assignment]
                ("only_one", lambda raw: raw.left == None),  # noqa: E711
            )

            id: int | None = Field(default=None, primary_key=True)
            left: str | None = None


def test_ferro_checks_must_be_a_tuple():
    with pytest.raises(TypeError, match="must be a tuple of Check"):

        class ListCheck(Model):
            __ferro_checks__: ClassVar[tuple[Check, ...]] = [  # ty: ignore[invalid-assignment]
                Check("only_one", lambda bad: bad.left == None)  # noqa: E711
            ]

            id: int | None = Field(default=None, primary_key=True)
            left: str | None = None


def test_collision_with_a_column_check_name_fails_at_class_definition():
    """A ``db_check`` column already owns ``ck_<table>_<col>``."""

    class Flavor(StrEnum):
        SWEET = "sweet"
        SALTY = "salty"

    with pytest.raises(TypeError, match="ck_colliding_flavor"):

        class Colliding(Model):
            __ferro_checks__: ClassVar[tuple[Check, ...]] = (
                Check("flavor", lambda colliding: colliding.memo == None),  # noqa: E711
            )

            id: int | None = Field(default=None, primary_key=True)
            flavor: Annotated[Flavor, Field(db_type="text", db_check=True)] = Flavor.SWEET
            memo: str | None = None


def test_empty_ferro_checks_is_a_no_op():
    class NoChecks(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = ()

        id: int | None = Field(default=None, primary_key=True)
        memo: str | None = None

    assert "table_checks" not in _model_payload("nochecks")


@pytest.mark.parametrize(
    "predicate, expected",
    [
        (lambda t: t.owner.label == None, "traversal"),  # noqa: E711
        (lambda t: t.notes.exists(), "existence test"),
        (lambda t: t.amount.sum(), "aggregate"),
    ],
)
def test_unsupported_check_predicates_fail_at_class_definition(predicate, expected):
    """Traversal, existence tests, and aggregates stay outside the dialect."""

    class RejectOwner(Model):
        id: int | None = Field(default=None, primary_key=True)
        label: str | None = None
        rejects: Relation[list["Rejecting"]] = BackRef()

    with pytest.raises(TypeError, match=expected):

        class Rejecting(Model):
            __ferro_checks__: ClassVar[tuple[Check, ...]] = (Check("rule", predicate),)

            id: int | None = Field(default=None, primary_key=True)
            memo: str | None = None
            amount: int = 0
            owner: Annotated[RejectOwner | None, ForeignKey(related_name="rejects")] = None
            notes: Relation[list["RejectNote"]] = BackRef()

        class RejectNote(Model):
            id: int | None = Field(default=None, primary_key=True)
            rejecting: Annotated[Rejecting, ForeignKey(related_name="notes")]


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def _build_transfer_models():
    """The Transfer-shaped fixture: four checks over two exclusive-or sides.

    Both FK-null spellings appear (the relation on the outflow checks, the
    shadow ``*_id`` column on the inflow ones), as do every combinator this
    release supports.
    """

    class Activity(Model):
        id: int | None = Field(default=None, primary_key=True)
        label: str = ""
        outflow_transfers: Relation[list["Transfer"]] = BackRef()
        inflow_transfers: Relation[list["Transfer"]] = BackRef()

    class Txn(Model):
        id: int | None = Field(default=None, primary_key=True)
        label: str = ""
        outflow_transfers: Relation[list["Transfer"]] = BackRef()
        inflow_transfers: Relation[list["Transfer"]] = BackRef()

    class Transfer(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check(
                "at_most_one_outflow",
                lambda transfer: (transfer.outflow_transaction == None)  # noqa: E711
                | (transfer.outflow_activity == None),  # noqa: E711
            ),
            Check(
                "at_most_one_inflow",
                lambda transfer: (transfer.inflow_transaction_id == None)  # noqa: E711
                | (transfer.inflow_activity_id == None),  # noqa: E711
            ),
            Check(
                "outflow_present",
                lambda transfer: ~(
                    (transfer.outflow_transaction == None)  # noqa: E711
                    & (transfer.outflow_activity == None)  # noqa: E711
                ),
            ),
            Check(
                "inflow_present",
                lambda transfer: (transfer.inflow_transaction_id != None)  # noqa: E711
                | (transfer.inflow_activity_id != None),  # noqa: E711
            ),
        )

        id: int | None = Field(default=None, primary_key=True)
        outflow_transaction: Annotated[
            Txn | None, ForeignKey(related_name="outflow_transfers")
        ] = None
        outflow_activity: Annotated[
            Activity | None, ForeignKey(related_name="outflow_transfers")
        ] = None
        inflow_transaction: Annotated[
            Txn | None, ForeignKey(related_name="inflow_transfers")
        ] = None
        inflow_activity: Annotated[
            Activity | None, ForeignKey(related_name="inflow_transfers")
        ] = None

    return Activity, Txn, Transfer


def test_compiler_emits_named_structured_table_checks():
    _build_transfer_models()

    table_checks = _model_payload("transfer")["table_checks"]

    assert [entry["name"] for entry in table_checks] == [
        "ck_transfer_at_most_one_outflow",
        "ck_transfer_at_most_one_inflow",
        "ck_transfer_outflow_present",
        "ck_transfer_inflow_present",
    ]
    # Structured predicates only — a raw SQL body in IR would make the two
    # emitters render from different sources (I-1).
    assert table_checks[0]["predicate"] == {
        "kind": "or",
        "left": {"kind": "is_null", "column": "outflow_transaction_id"},
        "right": {"kind": "is_null", "column": "outflow_activity_id"},
    }
    assert table_checks[2]["predicate"] == {
        "kind": "not",
        "child": {
            "kind": "and",
            "left": {"kind": "is_null", "column": "outflow_transaction_id"},
            "right": {"kind": "is_null", "column": "outflow_activity_id"},
        },
    }
    assert table_checks[3]["predicate"] == {
        "kind": "or",
        "left": {"kind": "is_not_null", "column": "inflow_transaction_id"},
        "right": {"kind": "is_not_null", "column": "inflow_activity_id"},
    }
    for entry in table_checks:
        assert set(entry) == {"name", "predicate"}


def test_both_fk_null_spellings_compile_to_the_same_predicate():
    """``t.outflow_transaction == None`` and ``t.outflow_transaction_id == None``
    are the same shadow-column IS NULL leaf (ADR-0016)."""

    class SpellTarget(Model):
        id: int | None = Field(default=None, primary_key=True)
        spellings: Relation[list["Spelling"]] = BackRef()

    class Spelling(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("via_relation", lambda spelling: spelling.target == None),  # noqa: E711
            Check("via_shadow", lambda spelling: spelling.target_id == None),  # noqa: E711
        )

        id: int | None = Field(default=None, primary_key=True)
        target: Annotated[SpellTarget | None, ForeignKey(related_name="spellings")] = None

    table_checks = _model_payload("spelling")["table_checks"]
    assert table_checks[0]["predicate"] == table_checks[1]["predicate"]
    assert table_checks[0]["predicate"] == {"kind": "is_null", "column": "target_id"}


def test_forward_referenced_relation_spelling_compiles():
    """A relation whose target resolves only at ``resolve_relationships`` still
    compiles: the shadow column name never needs the target class."""

    class Forward(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("target_absent", lambda forward: forward.later == None),  # noqa: E711
        )

        id: int | None = Field(default=None, primary_key=True)
        later: Annotated["LaterTarget", ForeignKey(related_name="forwards", nullable=True)] = (
            None
        )

    class LaterTarget(Model):
        id: int | None = Field(default=None, primary_key=True)
        forwards: Relation[list[Forward]] = BackRef()

    ensure_resolved_modelset()
    assert _model_payload("forward")["table_checks"] == [
        {
            "name": "ck_forward_target_absent",
            "predicate": {"kind": "is_null", "column": "later_id"},
        }
    ]


# ---------------------------------------------------------------------------
# Emission parity (I-1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_create_table_inlines_the_named_check_on_both_dialects(dialect):
    _build_transfer_models()
    payload = compile_registry_schema_ir()["payload"]

    create_sql, post_create_sqls, _pre = _render_create_table_sql_for_test(
        "Transfer", json.dumps(payload), dialect
    )

    assert f'CONSTRAINT "{OUTFLOW_CHECK_NAME}" CHECK ({OUTFLOW_CHECK_BODY})' in create_sql
    assert not any("CHECK" in sql for sql in post_create_sqls), post_create_sqls


def test_alembic_metadata_matches_the_runtime_check_names_and_bodies():
    """I-1: the two emitters render the same names and the same bodies."""
    _build_transfer_models()
    payload = compile_registry_schema_ir()["payload"]
    create_sql, _post, _pre = _render_create_table_sql_for_test(
        "Transfer", json.dumps(payload), "postgres"
    )

    table = get_metadata().tables["transfer"]
    constraints = {
        c.name: str(c.sqltext)
        for c in table.constraints
        if isinstance(c, sa.CheckConstraint)
    }

    ir_checks = _model_payload("transfer")["table_checks"]
    assert set(constraints) == {entry["name"] for entry in ir_checks}
    for entry in ir_checks:
        body = _render_table_check_body(json.dumps(entry["predicate"]))
        assert constraints[entry["name"]] == body
        assert f'CONSTRAINT "{entry["name"]}" CHECK ({body})' in create_sql


def test_db_check_column_check_stays_out_of_the_create_table_body():
    """``Field(db_check=True)`` keeps its ALTER-shaped path: emitted after
    CREATE on Postgres, elided on SQLite. Table checks did not change it."""

    class Flavor(StrEnum):
        SWEET = "sweet"
        SALTY = "salty"

    class Cookie(Model):
        id: int | None = Field(default=None, primary_key=True)
        flavor: Annotated[Flavor, Field(db_type="text", db_check=True)] = Flavor.SWEET

    payload = json.dumps(compile_registry_schema_ir()["payload"])

    pg_create, pg_post, _ = _render_create_table_sql_for_test("Cookie", payload, "postgres")
    assert "CHECK" not in pg_create
    assert any("ck_cookie_flavor" in sql for sql in pg_post)

    lite_create, lite_post, _ = _render_create_table_sql_for_test("Cookie", payload, "sqlite")
    assert "CHECK" not in lite_create
    assert not any("CHECK" in sql for sql in lite_post), lite_post


# ---------------------------------------------------------------------------
# Live behavior
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_transfer_checks_are_enforced_live(db_url, db_backend):
    """Four live CHECKs: legal transfers save, illegal ones raise
    ``CheckViolationError``. On Postgres the exception names the live
    constraint; SQLite's driver does not report one (typed error only)."""
    _Activity, Txn, Transfer = _build_transfer_models()

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        out_txn = await Txn.create(label="out")
        in_txn = await Txn.create(label="in")
        activity = await _Activity.create(label="opposite")

        # Legal: a transaction on each side.
        await Transfer.create(outflow_transaction=out_txn, inflow_transaction=in_txn)
        # Legal: a transaction plus an opposite-side activity.
        await Transfer.create(outflow_transaction=out_txn, inflow_activity=activity)

        # Illegal: both outflow spellings set.
        with pytest.raises(CheckViolationError) as excinfo:
            await Transfer.create(
                outflow_transaction=out_txn,
                outflow_activity=activity,
                inflow_transaction=in_txn,
            )
        if db_backend == "postgres":
            assert excinfo.value.constraint == OUTFLOW_CHECK_NAME

        # Illegal: no inflow at all.
        with pytest.raises(CheckViolationError):
            await Transfer.create(outflow_transaction=out_txn)


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_live_table_has_every_declared_check(db_url, db_backend):
    _build_transfer_models()
    await connect(db_url, auto_migrate=True)

    from ferro import fetch_all

    async with engines.session():
        if db_backend == "postgres":
            rows = await fetch_all(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'transfer'::regclass "
                "AND contype = 'c'"
            )
            names = {row["conname"] for row in rows}
        else:
            rows = await fetch_all(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transfer'"
            )
            ddl = rows[0]["sql"]
            names = {
                name
                for name in (
                    "ck_transfer_at_most_one_outflow",
                    "ck_transfer_at_most_one_inflow",
                    "ck_transfer_outflow_present",
                    "ck_transfer_inflow_present",
                )
                if f'CONSTRAINT "{name}" CHECK' in ddl
            }

    assert names >= {
        "ck_transfer_at_most_one_outflow",
        "ck_transfer_at_most_one_inflow",
        "ck_transfer_outflow_present",
        "ck_transfer_inflow_present",
    }


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_auto_migrate_does_not_add_a_check_to_an_existing_table(db_url):
    """ADR-0010: without ``migrate_updates`` the create pass owns only missing
    tables. Adding a check to an existing table is #343's job."""
    from ferro.registry import REGISTRY

    class Reconcile(Model):
        id: int | None = Field(default=None, primary_key=True)
        left: str | None = None
        right: str | None = None

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Reconcile.create(left="a", right="b")

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()

    class Reconcile(Model):  # noqa: F811 — the same table, now with a check
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check(
                "at_most_one_side",
                lambda reconcile: (reconcile.left == None)  # noqa: E711
                | (reconcile.right == None),  # noqa: E711
            ),
        )

        id: int | None = Field(default=None, primary_key=True)
        left: str | None = None
        right: str | None = None

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        # The live table has no CHECK, so a row violating the declared
        # predicate still inserts: auto_migrate left the table untouched.
        row = await Reconcile.create(left="a", right="b")
        assert row.id is not None


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_autogenerate_against_a_rust_bootstrapped_db_is_empty(
    db_url, postgres_base_url, db_schema_name
):
    """I-1 sentinel for table checks: Alembic must see no drift."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    _build_transfer_models()
    await connect(db_url, auto_migrate=True)

    metadata = get_metadata()

    if db_url.startswith("sqlite:"):
        db_path = db_url.replace("sqlite:", "", 1).split("?")[0]
        engine = sa.create_engine(f"sqlite:///{db_path}")
        search_path_schema = None
    else:
        for scheme in ("postgresql://", "postgres://"):
            if postgres_base_url.startswith(scheme):
                sync_url = "postgresql+psycopg://" + postgres_base_url[len(scheme) :]
                break
        else:
            sync_url = postgres_base_url
        engine = sa.create_engine(sync_url)
        search_path_schema = db_schema_name

    try:
        with engine.connect() as conn:
            if search_path_schema is not None:
                conn.execute(sa.text(f'SET search_path TO "{search_path_schema}"'))
            ctx = MigrationContext.configure(
                conn, opts={"compare_type": True, "compare_server_default": True}
            )
            diff = compare_metadata(ctx, metadata)
    finally:
        engine.dispose()

    flat = [op for entry in diff for op in (entry if isinstance(entry, list) else [entry])]
    assert flat == [], f"table checks produced a phantom autogenerate diff:\n{flat}"


def test_ferro_exports_check():
    import ferro

    assert ferro.Check is Check
    assert "Check" in ferro.__all__


def test_field_helpers_still_import_cleanly():
    """A leaf sanity pin: ``Check`` lives beside the other declaration helpers."""
    assert FerroField is not None
