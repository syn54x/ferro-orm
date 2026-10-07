"""Cross-emitter DDL parity sentinels (invariant I-1 in AGENTS.md).

These tests guard the project invariant that every DDL emission path in Ferro
produces equivalent schema artifacts for the same model definition. The two
emitters today are the Alembic autogenerate bridge (Python) and the Rust
runtime DDL emitter (`src/schema.rs`).

The canonical test in this file is
``test_alembic_autogen_against_rust_migrated_db_is_idempotent``: it bootstraps
a fresh database via Rust runtime DDL, then asks Alembic's metadata-comparison
engine whether it would propose any migration ops. An empty diff means both
emitters agree about every artifact in the fixture model — exactly the
property that prevents phantom drop+create diffs in real-world migration
flows.

Since ADR-0041 the bridge has one decider: autogenerate reads the live
database through the reconciliation pass's converter and asks the one
planner, so an empty autogenerate and "no drift" are the same statement and
no diff is ever filtered here. When you add a new schema feature, extend the
fixture model below to cover it; if the sentinel goes red, the planner and
the create pass disagree — fix them.
"""

import datetime
import decimal
import uuid
from enum import StrEnum
from typing import Annotated, ClassVar

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from ferro import (
    BackRef,
    FerroField,
    ForeignKey,
    Model,
    Relation,
    clear_registry,
    connect,
    reset_engine,
)
from ferro.migrations import get_metadata
from tests._alembic_harness import autogen_opts

pytestmark = pytest.mark.backend_matrix


@pytest.fixture(autouse=True)
def cleanup():
    from ferro.registry import REGISTRY

    REGISTRY.reset_for_test()
    reset_engine()
    clear_registry()
    yield
    REGISTRY.reset_for_test()


class OrgRole(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


def _build_fixture_models() -> None:
    """Define a model graph that exercises every cross-emitter artifact,
    including the full derived-type family (FF-B B5).

    Defined inside a helper so the cleanup fixture can clear the registry
    cleanly between runs without leaving dangling class references.
    """

    class Org(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(index=True)]
        slug: Annotated[str, FerroField(unique=True)]
        role: OrgRole  # native PG enum / varchar(max label len) on SQLite
        created_at: datetime.datetime  # timestamptz
        founded: datetime.date  # date
        opens_at: datetime.time  # time
        token: uuid.UUID  # uuid
        balance: decimal.Decimal  # numeric
        avatar: bytes  # bytea/blob
        settings: dict  # json
        score: float  # double precision
        members: Relation[list["Member"]] = BackRef()
        projects: Relation[list["Project"]] = BackRef()

    class Member(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: Annotated[str, FerroField(unique=True)]
        org: Annotated[Org, ForeignKey(related_name="members", index=True)]

        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("email", "org_id"),
        )

    class Project(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        org: Annotated[Org, ForeignKey(related_name="projects", index=True)]

        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("org_id", "name"),
        )

    # Reference the names so static analyzers don't strip the bodies.
    return Org, Member, Project


@pytest.mark.asyncio
async def test_alembic_autogen_against_rust_migrated_db_is_idempotent(
    db_url, postgres_base_url, db_schema_name
):
    """Schema-drift sentinel: Alembic must see a Rust-migrated DB as up-to-date.

    This is the canonical guard on the cross-emitter DDL parity invariant
    (I-1 in AGENTS.md), running on the FULL backend matrix (FF-B B5) — the
    derived-type divergences it guards (native enums, timestamptz) can only
    fail on Postgres. If the two emitters disagree about any schema artifact
    - index name, type, nullability, constraint name, default - this test
    fails with a non-empty diff describing the disagreement, with ZERO
    filters hiding type-family diffs.

    The fixture model deliberately covers:
    - The full derived-type family: Enum, datetime, date, time, UUID,
      Decimal, bytes, JSON, float
    - Single-column ``FerroField(index=True)`` / ``FerroField(unique=True)``
    - Shadow-column ``ForeignKey(index=True)`` (the issue-32 surface)
    - ``ForeignKey`` without ``index=True`` (default, no extra index)
    - ``__ferro_composite_indexes__`` and ``__ferro_composite_uniques__``
    - Mixed FK target (Org is referenced from two distinct tables)
    """
    _build_fixture_models()

    await connect(db_url, auto_migrate=True)

    metadata = get_metadata()

    if db_url.startswith("sqlite:"):
        db_path = db_url.replace("sqlite:", "", 1).split("?")[0]
        engine = sa.create_engine(f"sqlite:///{db_path}")
        search_path_schema = None
    else:
        # The per-test schema is carried in a Ferro-specific URL param; the
        # plain SQLAlchemy engine needs the base URL plus an explicit
        # search_path (mirrors test_schema_constraints.py). Force the psycopg
        # (v3) driver regardless of the incoming scheme — the base URL is
        # ``postgresql://`` from pytest-postgresql but ``postgres://`` from a
        # ``FERRO_POSTGRES_URL`` env override, and SQLAlchemy's bare
        # ``postgresql://`` defaults to psycopg2, which Ferro does not ship.
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
            ctx = MigrationContext.configure(conn, opts=autogen_opts())
            diff = compare_metadata(ctx, metadata)
    finally:
        engine.dispose()

    significant = diff
    assert significant == [], (
        "Cross-emitter DDL parity violation: Alembic compare_metadata against "
        "a Rust-migrated database returned a non-empty diff. The two emitters "
        "disagree about the schema; running `alembic revision --autogenerate` "
        "against an auto_migrate'd database would produce phantom diffs.\n\n"
        f"Diff:\n{significant}"
    )


def _build_migration_v1_models() -> None:
    """The 'old release' shape of the migration-sentinel models."""

    class MigOrg(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        slug: Annotated[str, FerroField(unique=True)]
        members: Relation[list["MigMember"]] = BackRef()

    class MigMember(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: Annotated[str, FerroField(unique=True)]
        org: Annotated[MigOrg, ForeignKey(related_name="members", index=True)]

    return MigOrg, MigMember


def _build_migration_v2_models() -> None:
    """The 'new release' shape: MigOrg gained two columns since v1."""

    class MigOrg(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        slug: Annotated[str, FerroField(unique=True)]
        name: Annotated[str | None, FerroField(index=True)] = None
        motto: str | None = None
        members: Relation[list["MigMember"]] = BackRef()

    class MigMember(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: Annotated[str, FerroField(unique=True)]
        org: Annotated[MigOrg, ForeignKey(related_name="members", index=True)]

    return MigOrg, MigMember


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_alembic_autogen_after_migrate_updates_is_idempotent(db_url):
    """Migration-path parity sentinel: a database bootstrapped on an old model
    shape and brought forward via ``connect(migrate_updates=True)`` must be
    indistinguishable to Alembic from one created fresh.

    This pins that ``ALTER TABLE ... ADD COLUMN`` reuses the exact column DDL
    of the CREATE TABLE emitter — including single-column index names — so the
    auto-migrate path cannot drift from the Alembic bridge (I-1 in AGENTS.md).

    Scope note: the v1→v2 delta covers a plain nullable column and an indexed
    nullable column, where byte parity is achievable on SQLite. Unique and
    foreign-key column adds are deliberately not part of this sentinel on
    SQLite: the engine cannot add inline UNIQUE/FK constraints to existing
    tables, so auto-migrate emits the documented equivalent (an explicit
    ``uq_`` index / a constraint-less column) plus a ``UserWarning`` — visible
    divergence by design, covered in ``test_auto_migrate.py``.
    """
    from ferro.registry import REGISTRY

    _build_migration_v1_models()
    await connect(db_url, auto_migrate=True)

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()

    _build_migration_v2_models()
    await connect(db_url, migrate_updates=True)

    metadata = get_metadata()

    db_path = db_url.replace("sqlite:", "", 1).split("?")[0]
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=autogen_opts())
            diff = compare_metadata(ctx, metadata)
    finally:
        engine.dispose()

    significant = diff
    assert significant == [], (
        "Cross-emitter DDL parity violation on the migrate_updates path: a "
        "database migrated forward from an older model shape differs from "
        "what Alembic expects of the current models.\n\n"
        f"Diff:\n{significant}"
    )


def _provider_envelope(labels: list[str]) -> dict:
    """A one-table modelset whose ``provider`` column is a native enum."""
    column = {
        "logical_type": "string",
        "nullable": False,
        "primary_key": False,
        "autoincrement": False,
        "unique": False,
        "index": False,
        "default": None,
        "format": None,
    }
    return {
        "ir_kind": "schema",
        "ir_version": 2,
        "payload": {
            "dialect_agnostic": True,
            "models": [
                {
                    "model_name": "app.Link",
                    "table_name": "link",
                    "columns": [
                        {
                            **column,
                            "name": "id",
                            "logical_type": "integer",
                            "primary_key": True,
                        },
                        {
                            **column,
                            "name": "provider",
                            "enum_values": labels,
                            "enum_type_name": "provider",
                        },
                    ],
                    "foreign_keys": [],
                    "indexes": [],
                    "uniques": [],
                    "checks": [],
                    "table_checks": [],
                }
            ],
        },
    }


def test_label_addition_statement_parity_pin():
    """Cross-language golden pin for label addition (AGENTS.md § I-1 item 11).

    The one planner renders the ``ADD VALUE IF NOT EXISTS`` statement
    byte-for-byte — the same literal is pinned in ferro-ddl-lowering's unit
    tests, the reconciliation pass executes it, and the Alembic bridge writes
    it verbatim (ADR-0041). If any side drifts, the migration doors would run
    different SQL for the same model; this pin fails first.
    """
    import json

    from ferro._core import _plan_from_ir

    plan = json.loads(
        _plan_from_ir(
            json.dumps(_provider_envelope(["plaid", "legacy"])),
            json.dumps(_provider_envelope(["plaid", "mx"])),
            "postgres",
            json.dumps({"destructive": True}),
            True,
        )
    )
    assert [(op["kind"], op["statements"]) for op in plan["operations"]] == [
        ("AddEnumLabel", ["ALTER TYPE \"provider\" ADD VALUE IF NOT EXISTS 'mx'"])
    ]
    assert any("legacy" in warning for warning in plan["warnings"])


def test_enum_type_provenance_parity_pin():
    """Cross-language golden pin for the type-provenance decision (AGENTS.md
    § I-1 item 17; ADR-0020, ADR-0021; #438, #443).

    The FFI returns the Rust-rendered ``DROP TYPE`` — and, for a type only
    ``add_column`` introduces, the guarded ``CREATE TYPE`` the auto-migrate
    create pass executes (#439) — byte-for-byte: the same literals are
    pinned in ferro-ddl-lowering's unit tests, and the planner's enum ops
    render the same statements on every door. The decision
    is pinned alongside, one verdict per touched type: a type is the
    revision's (``introduced``: created on upgrade, dropped on downgrade)
    when every column declaring it is one the revision adds; a type with an
    added column but a surviving one too is ``reused`` (its created-table
    columns render ``create_type=False``, no drop, no create).
    ``categorycolor`` (created table plus ``add_column``) and ``cardsize``
    (created table) are introduced and created inline by ``create_table``;
    ``memberkind`` (``add_column`` only) is introduced and created by
    statement; ``ledgerrole`` keeps a pre-existing column on ``member`` and
    ``accountkind`` keeps one of its two columns, so both are reused. A type
    with no added column is absent.
    """
    import json

    from ferro._core import _plan_enum_type_provenance

    plan = json.loads(
        _plan_enum_type_provenance(
            json.dumps(
                {
                    "categorycolor": [["category", "color"], ["card", "color"]],
                    "cardsize": [["card", "size"]],
                    "ledgerrole": [["ledger", "role"], ["member", "role"]],
                    "memberkind": [["member", "kind"]],
                    "accountkind": [["account", "kind"], ["account", "legacy_kind"]],
                    "untouched": [["ledger", "status"]],
                }
            ),
            json.dumps(
                [
                    ["category", "color"],
                    ["card", "color"],
                    ["card", "size"],
                    ["ledger", "role"],
                    ["member", "kind"],
                    ["account", "kind"],
                ]
            ),
            # The revision creates `category` and `card`; every other
            # table survives.
            json.dumps([["category", "color"], ["card", "color"], ["card", "size"]]),
            json.dumps(
                {
                    "categorycolor": ["rust", "amber"],
                    "cardsize": ["small", "large"],
                    "ledgerrole": ["owner", "guest"],
                    "memberkind": ["person", "org"],
                    "accountkind": ["asset", "liability"],
                    "untouched": ["open", "closed"],
                }
            ),
        )
    )
    assert plan == [
        {
            "name": "accountkind",
            "provenance": "reused",
            "create_statement": None,
            "drop_statement": None,
        },
        {
            "name": "cardsize",
            "provenance": "introduced",
            "create_statement": None,
            "drop_statement": 'DROP TYPE "cardsize"',
        },
        {
            "name": "categorycolor",
            "provenance": "introduced",
            "create_statement": None,
            "drop_statement": 'DROP TYPE "categorycolor"',
        },
        {
            "name": "ledgerrole",
            "provenance": "reused",
            "create_statement": None,
            "drop_statement": None,
        },
        {
            "name": "memberkind",
            "provenance": "introduced",
            "create_statement": (
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typname = 'memberkind' AND n.nspname = current_schema()) "
                "THEN CREATE TYPE \"memberkind\" AS ENUM ('person', 'org'); "
                "END IF; END $$"
            ),
            "drop_statement": 'DROP TYPE "memberkind"',
        },
    ]
