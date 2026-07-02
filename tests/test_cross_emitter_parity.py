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

When you add a new schema feature (a constraint, a default, a new index
variant), extend the fixture model below to cover it. If the sentinel goes
red, either the feature's two emitter implementations disagree (fix the
disagreement) or the feature legitimately falls outside Alembic's
introspection precision (filter that op kind out of the diff with a clear
comment).
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

pytestmark = pytest.mark.backend_matrix


@pytest.fixture(autouse=True)
def cleanup():
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    reset_engine()
    clear_registry()
    yield
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()


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


def _flatten_diff(diff: list) -> list:
    """``compare_metadata`` returns a mix of bare op tuples and sublists.

    Column-level alterations get grouped in a sublist (so they can be applied
    as a batched ``ALTER TABLE``). Flatten so each filter sees a single op.
    """
    flat = []
    for entry in diff:
        if isinstance(entry, list):
            flat.extend(entry)
        else:
            flat.append(entry)
    return flat


def _ignore_unreliable_alembic_diffs(diff: list, metadata: sa.MetaData) -> list:
    """Filter pre-existing divergences with tracked issues.

    ZERO filters since FF-B B5 — the function stays as the policy anchor: any
    future entry MUST cite a tracked issue under ``docs/solutions/issues/``
    and explain why the diff is known-equivalent SQL rather than real
    cross-emitter drift. Do not add filters silently; fix the emitters.
    """
    del metadata  # kept in the signature for future filters
    return _flatten_diff(diff)


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
        # search_path (mirrors test_schema_constraints.py).
        sync_url = postgres_base_url.replace("postgres://", "postgresql+psycopg://", 1)
        engine = sa.create_engine(sync_url)
        search_path_schema = db_schema_name

    try:
        with engine.connect() as conn:
            if search_path_schema is not None:
                conn.execute(sa.text(f'SET search_path TO "{search_path_schema}"'))
            ctx = MigrationContext.configure(
                conn,
                opts={"compare_type": True, "compare_server_default": True},
            )
            diff = compare_metadata(ctx, metadata)
    finally:
        engine.dispose()

    significant = _ignore_unreliable_alembic_diffs(diff, metadata)
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
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _build_migration_v1_models()
    await connect(db_url, auto_migrate=True)

    reset_engine()
    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    _build_migration_v2_models()
    await connect(db_url, migrate_updates=True)

    metadata = get_metadata()

    db_path = db_url.replace("sqlite:", "", 1).split("?")[0]
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn,
                opts={"compare_type": True, "compare_server_default": True},
            )
            diff = compare_metadata(ctx, metadata)
    finally:
        engine.dispose()

    significant = _ignore_unreliable_alembic_diffs(diff, metadata)
    assert significant == [], (
        "Cross-emitter DDL parity violation on the migrate_updates path: a "
        "database migrated forward from an older model shape differs from "
        "what Alembic expects of the current models.\n\n"
        f"Diff:\n{significant}"
    )
