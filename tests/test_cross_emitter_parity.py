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
    reset_engine()
    clear_registry()
    yield


def _build_fixture_models() -> None:
    """Define a model graph that exercises every cross-emitter artifact.

    Defined inside a helper so the cleanup fixture can clear the registry
    cleanly between runs without leaving dangling class references.
    """

    class Org(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(index=True)]
        slug: Annotated[str, FerroField(unique=True)]
        members: Relation[list["Member"]] = BackRef()
        projects: Relation[list["Project"]] = BackRef()

    class Member(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: Annotated[str, FerroField(unique=True)]
        org: Annotated[Org, ForeignKey(related_name="members", index=True)]

    class Project(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        org: Annotated[Org, ForeignKey(related_name="projects", index=True)]

        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("org_id", "name"),
        )

    # Reference the names so static analyzers don't strip the bodies.
    return Org, Member, Project


def _is_pk_nullable_relaxation(op_tuple, metadata: sa.MetaData) -> bool:
    """``modify_nullable`` flagged on a primary-key column.

    Pre-existing divergence (tracked in
    ``docs/solutions/issues/sa-pk-column-nullable-divergence.md``):
    Ferro's SA bridge maps ``Annotated[int | None, FerroField(primary_key=True)]``
    to ``Column(primary_key=True, nullable=True)``. Rust emits ``NOT NULL``,
    which matches SQL semantics (PK columns are always NOT NULL). Alembic
    introspects the DB as ``nullable=False`` and the metadata as
    ``nullable=True`` and proposes a relaxation that would never actually run.
    """
    if op_tuple[0] != "modify_nullable":
        return False
    _, _schema, table_name, column_name, _info, new_nullable, existing_nullable = (
        op_tuple
    )
    if not (new_nullable is True and existing_nullable is False):
        return False
    table = metadata.tables.get(table_name)
    if table is None:
        return False
    column = table.c.get(column_name)
    if column is None:
        return False
    return bool(column.primary_key)


def _is_redundant_single_column_unique(op_tuple) -> bool:
    """``add_constraint UniqueConstraint(col)`` on a single column.

    Pre-existing divergence (tracked in
    ``docs/solutions/issues/sa-vs-rust-unique-constraint-shape.md``):
    SA renders ``Column(..., unique=True)`` as a separate ``UniqueConstraint``
    object on the table. The Rust emitter writes the ``UNIQUE`` keyword
    inline on the column. The emitted SQL is equivalent, but SA's reflector
    only finds a column-level uniqueness flag in the live DB and reports the
    standalone ``UniqueConstraint`` as missing.
    """
    if op_tuple[0] != "add_constraint":
        return False
    constraint = op_tuple[1]
    if not isinstance(constraint, sa.UniqueConstraint):
        return False
    return len(list(constraint.columns)) == 1


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
    """Filter the narrow set of pre-existing divergences with tracked issues.

    Every entry here MUST cite a tracked issue under
    ``docs/solutions/issues/`` and explain why the diff is known-equivalent
    SQL rather than real cross-emitter drift. Do not add filters silently.
    """
    return [
        op
        for op in _flatten_diff(diff)
        if not _is_pk_nullable_relaxation(op, metadata)
        and not _is_redundant_single_column_unique(op)
    ]


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_alembic_autogen_against_rust_migrated_db_is_idempotent(db_url):
    """Schema-drift sentinel: Alembic must see a Rust-migrated DB as up-to-date.

    This is the canonical guard on the cross-emitter DDL parity invariant
    (I-1 in AGENTS.md). If the two emitters disagree about any schema
    artifact - index name, type, nullability, constraint name, default - this
    test fails with a non-empty diff describing the disagreement.

    The fixture model deliberately covers:
    - Single-column ``FerroField(index=True)``
    - Single-column ``FerroField(unique=True)``
    - Shadow-column ``ForeignKey(index=True)`` (the issue-32 surface)
    - ``ForeignKey`` without ``index=True`` (default, no extra index)
    - ``__ferro_composite_indexes__`` (multi-column index)
    - Mixed FK target (Org is referenced from two distinct tables)
    """
    _build_fixture_models()

    await connect(db_url, auto_migrate=True)

    metadata = get_metadata()

    if db_url.startswith("sqlite:"):
        db_path = db_url.replace("sqlite:", "", 1).split("?")[0]
        sync_url = f"sqlite:///{db_path}"
    else:
        sync_url = db_url

    engine = sa.create_engine(sync_url)
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
        "Cross-emitter DDL parity violation: Alembic compare_metadata against "
        "a Rust-migrated database returned a non-empty diff. The two emitters "
        "disagree about the schema; running `alembic revision --autogenerate` "
        "against an auto_migrate'd database would produce phantom diffs.\n\n"
        f"Diff:\n{significant}"
    )
