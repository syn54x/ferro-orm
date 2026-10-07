"""Cross-emitter DDL parity sentinels (invariant I-1 in AGENTS.md).

These tests guard the project invariant that every DDL emission path in Ferro
produces equivalent schema artifacts for the same model definition. The
emitters are the reconciliation pass (``connect(auto_migrate=True)``, the Rust
core), the Alembic autogenerate bridge, and the migrations door
(``ferro migrate new``: ``src/ferro/migrations/`` and
``crates/ferro-migrate/src/generate/``).

```python
class Author(Model):
    ...
    bio: str | None = None   # the edit
```

```text
ferro migrate new          01_schema.up.postgres.sql   ALTER TABLE "author" ADD COLUMN "bio" varchar;
connect(migrate_updates)   executes                    ALTER TABLE "author" ADD COLUMN "bio" varchar
alembic --autogenerate     upgrade()                   op.add_column('author', sa.Column('bio', ...))
```

The migrations door is pinned by six pins over every casebook change
(``tests/_casebook.py``) on both dialects — (a) the generated steps are the
pass's plan, the online shapes compared by their plain twins
(:func:`normalize_online_shape`); (b) a rebuild's ``CREATE TABLE`` is the
create pass's; (c) a concurrent index build is the pass's statement but for
one token; (d) the validate, label-addition and type-creation statements are
the pass's; (e) a migrated database equals an ``auto_migrate``'d one and
Alembic sees nothing to do against either; (f) the bridge's revision runs the
pass's DDL — at the end of this file.

The canonical bridge test in this file is
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

import contextlib
import dataclasses
import datetime
import decimal
import io
import json
import re
import shutil
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
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
    _core,
    clear_registry,
    connect,
    reset_engine,
)
from ferro.migrations import get_metadata
from tests._alembic_harness import autogen_opts
from tests._casebook import CASES, Case
from tests.test_migrate_down import migration_dir
from tests.test_migrate_new import statements, write_config, write_models
from tests.test_migrate_up import new

pytestmark = pytest.mark.backend_matrix

DESTRUCTIVE = json.dumps({"destructive": True})


def _rewind_registry() -> None:
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


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
        ("AddEnumLabel", ["ALTER TYPE \"provider\" ADD VALUE IF NOT EXISTS 'mx'"]),
        # Between two declared snapshots a dropped label is the generator's
        # removal (#536); the pass renders it as ADR-0011's warning only.
        ("RemoveEnumLabel", []),
    ]
    assert any("legacy" in warning for warning in plan["operations"][1]["warnings"])


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


# ===========================================================================
# The migrations door (#538): the six pins over the casebook
# ===========================================================================
#
# ``ferro migrate new`` writes a casebook change into step files; the
# reconciliation pass plans the same before/after modelset with
# ``_core._plan_from_ir(parent, target, dialect, ..., render=True)`` (two
# declared snapshots: ``LiveFacts::declared()``). The generator writes some
# statements in an online shape the pass, inside its one transaction, never
# needs — ``NOT VALID`` then ``VALIDATE`` (ADR-0043), ``CONCURRENTLY``
# (ADR-0044), the ``_ferro_notnull_*`` staging (#534) — and on SQLite a
# table rebuild where ``ALTER TABLE`` has no statement (ADR-0046). Pin (a)
# compares each by its plain twin; pins (b)–(d) pin the online shapes
# themselves to the pass's renderings.

DIALECTS = ("postgres", "sqlite")
_STEP_FILE = re.compile(r"^(\d\d_[a-z0-9_]+)\.(up|down)\.(postgres|sqlite)\.sql$")
_DATA_STEP = re.compile(r"^(\d\d_[a-z0-9_]+)\.py$")
_TODO = re.compile(r"todo\((?:[^()]|\([^()]*\))*\)")
_PHASE_STEPS = {
    "labels",
    "schema",
    "expand",
    "add_constraint",
    "contract",
    "validate",
}


def normalize_online_shape(statement: str) -> str:
    """The statement the reconciliation pass runs for one generated statement:
    a ``NOT VALID`` add is the plain add, a ``CONCURRENTLY`` index statement
    is the plain one (``IF NOT EXISTS`` where the build was concurrent), and a
    validate or a ``_ferro_notnull_*`` staging statement has no twin (``""``):
    the pass adds every constraint validated and sets ``NOT NULL`` directly.
    Every other statement is its own twin."""
    if "_ferro_notnull_" in statement or " VALIDATE CONSTRAINT " in statement:
        return ""
    if " ADD CONSTRAINT " in statement:
        statement = re.sub(r" NOT VALID(?=;|$)", "", statement)
    statement = re.sub(
        r"^CREATE (UNIQUE )?INDEX CONCURRENTLY ",
        r"CREATE \1INDEX IF NOT EXISTS ",
        statement,
    )
    return statement.replace(
        "DROP INDEX CONCURRENTLY IF EXISTS ", "DROP INDEX IF EXISTS "
    )


def _plain_twins(statements: list[str]) -> list[str]:
    """A step's statements as the pass would run them. A concurrent build's
    leading ``DROP INDEX CONCURRENTLY IF EXISTS`` of the index it builds is
    the crash-leftover guard (ADR-0044), which no plain build needs."""
    out: list[str] = []
    for i, statement in enumerate(statements):
        following = statements[i + 1] if i + 1 < len(statements) else ""
        if statement.startswith("DROP INDEX CONCURRENTLY IF EXISTS "):
            name = statement.removeprefix("DROP INDEX CONCURRENTLY IF EXISTS ")
            if re.match(
                rf"^CREATE (UNIQUE )?INDEX CONCURRENTLY {re.escape(name)} ", following
            ):
                continue
        twin = normalize_online_shape(statement)
        if twin:
            out.append(twin)
    return out


@dataclass(frozen=True)
class Rebuild:
    """One object a step rebuilds because no statement alters it in place: a
    SQLite table (``CREATE TABLE "_ferro_new_<table>"`` through the indexes
    it re-creates, ADR-0046) or a Postgres enum type losing a label
    (``CREATE TYPE "<type>_new"`` through its rename, #536). ``subject`` is
    the table, or ``type:<type>``."""

    subject: str
    statements: tuple[str, ...]

    @property
    def table(self) -> str | None:
        return None if self.subject.startswith("type:") else self.subject

    @property
    def create(self) -> str:
        return self.statements[0]

    @property
    def indexes(self) -> list[str]:
        rename = self.statements.index(
            f'ALTER TABLE "_ferro_new_{self.table}" RENAME TO "{self.table}"'
        )
        return list(self.statements[rename + 1 :])


def _split_rebuilds(statements: list[str]) -> tuple[list[str], list[Rebuild]]:
    """A step's statements outside any rebuild, and its rebuilds."""
    natives: list[str] = []
    rebuilds: list[Rebuild] = []
    i = 0
    while i < len(statements):
        table = re.match(
            r'^CREATE TABLE IF NOT EXISTS "_ferro_new_([^"]+)" ', statements[i]
        )
        swap = re.match(r'^CREATE TYPE "([^"]+)_new" AS ENUM ', statements[i])
        if table is not None:
            subject, name = table[1], table[1]
            last = f'ALTER TABLE "_ferro_new_{name}" RENAME TO "{name}"'
        elif swap is not None:
            subject, name = f"type:{swap[1]}", swap[1]
            last = f'ALTER TYPE "{name}_new" RENAME TO "{name}"'
        else:
            natives.append(statements[i])
            i += 1
            continue
        assert last in statements[i:], (subject, statements)
        end = statements.index(last, i) + 1
        if table is not None:
            index = re.compile(
                rf'^CREATE (UNIQUE )?INDEX IF NOT EXISTS "[^"]+" ON "{re.escape(name)}" '
            )
            while end < len(statements) and index.match(statements[end]):
                end += 1
        rebuilds.append(Rebuild(subject, tuple(statements[i:end])))
        i = end
    return natives, rebuilds


def _op_subjects(op: dict) -> set[str]:
    """The tables (and ``type:<type>``) a planner op touches."""
    subjects = {op["table"]} if op.get("table") else set()
    if op["kind"] == "RenameTable":
        subjects |= {op["old"], op["new"]}
    if op.get("type_name"):
        subjects.add(f"type:{op['type_name']}")
    for pair in op.get("columns") or []:
        if isinstance(pair, list | tuple):
            subjects.add(pair[0])
    return subjects


def _empty(envelope: dict) -> dict:
    return {**envelope, "payload": {**envelope["payload"], "models": []}}


def _plan(old: dict, new: dict, dialect: str) -> list[dict]:
    """The reconciliation pass's rendered plan from ``old`` to ``new``, two
    declared snapshots, destructive changes on (the generator plans every
    drop)."""
    return json.loads(
        _core._plan_from_ir(
            json.dumps(old), json.dumps(new), dialect, DESTRUCTIVE, True
        )
    )["operations"]


@dataclass(frozen=True)
class Door:
    """One casebook change as ``ferro migrate new`` wrote it: ``0001`` creates
    ``case.before``, ``0002`` (``migration``) is the edit to ``case.after``."""

    case: Case
    migration: Path
    parent: dict
    target: dict
    data_steps: tuple[dict, ...]

    @property
    def stems(self) -> list[str]:
        """Every step, in order (``01_schema``, ``02_backfill_author``, …)."""
        stems = {
            m[1]
            for p in self.migration.iterdir()
            for m in (_STEP_FILE.match(p.name), _DATA_STEP.match(p.name))
            if m
        }
        return sorted(stems)

    def is_data(self, stem: str) -> bool:
        return (self.migration / f"{stem}.py").exists()

    def statements(self, stem: str, direction: str, dialect: str) -> list[str]:
        return statements(self.migration / f"{stem}.{direction}.{dialect}.sql")

    def text(self, stem: str, direction: str, dialect: str) -> str:
        return (self.migration / f"{stem}.{direction}.{dialect}.sql").read_text()

    @property
    def relaxed(self) -> dict | None:
        """The shape between the expand and the contract (ADR-0040): the
        target with every column a backfill fills ``NULL``s of nullable. A
        removed enum label demands values but relaxes nothing."""
        if not self.data_steps:
            return None
        filled = {
            (step["table"], column["name"])
            for step in self.data_steps
            for column in step["columns"]
            if column["reason"]["kind"] != "label_removed"
        }
        relaxed = json.loads(json.dumps(self.target))
        for model in relaxed["payload"]["models"]:
            for column in model["columns"]:
                if (model["table_name"], column["name"]) in filled:
                    column["nullable"] = True
        return relaxed

    @property
    def shapes(self) -> list[dict]:
        """The parent, the relaxed shape when there is a backfill, the target."""
        relaxed = self.relaxed
        return [self.parent, *([relaxed] if relaxed else []), self.target]

    def phase(self, stem: str) -> int:
        """0 before the backfill (or without one), 1 after it."""
        data = [s for s in self.stems if self.is_data(s)]
        return int(bool(data) and stem > data[0])

    def shape_left(self, stem: str, direction: str) -> dict:
        """The shape a step leaves: its phase's end going up, its start going
        down."""
        phase = self.phase(stem)
        return self.shapes[phase + 1 if direction == "up" else phase]

    def pass_plans(self, dialect: str) -> list[list[dict]]:
        """The pass's plan per phase: parent → target, or parent → relaxed →
        target around a backfill (the pass has no statement for a value
        existing rows lack, so it plans the two halves the backfill joins)."""
        shapes = self.shapes
        return [_plan(a, b, dialect) for a, b in zip(shapes, shapes[1:], strict=False)]


def _generate(case: Case, root: Path) -> Door:
    """Run ``ferro migrate new`` for ``case`` in a fresh project under
    ``root``, both dialects, in this process."""
    project = root / case.id
    project.mkdir()
    pkg = "ferro_p538_" + re.sub(r"[^a-z0-9]", "_", case.id.lower())
    out = io.StringIO()
    with pytest.MonkeyPatch.context() as mp, contextlib.redirect_stdout(out):
        mp.delenv("FERRO_CONFIG", raising=False)
        mp.chdir(project)
        mp.setattr(sys, "path", [str(project), *sys.path])
        mp.setattr(sys, "dont_write_bytecode", True)
        try:
            write_config(project, pkg)
            write_models(project, pkg, case.before)
            new("create")
            write_models(project, pkg, case.after)
            new("edit")
        finally:
            for name in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
                del sys.modules[name]
            _rewind_registry()
    migration = migration_dir(project, 2)
    parent_text = (migration_dir(project, 1) / "ir.json").read_text()
    target_text = (migration / "ir.json").read_text()
    generated = json.loads(
        _core._generate_migration(parent_text, target_text, list(DIALECTS))
    )
    return Door(
        case=case,
        migration=migration,
        parent=json.loads(parent_text),
        target=json.loads(target_text),
        data_steps=tuple(s["data"] for s in generated["steps"] if s.get("data")),
    )


@pytest.fixture(scope="session")
def doors(tmp_path_factory: pytest.TempPathFactory):
    """Each casebook change generated once per session, on first use."""
    root = tmp_path_factory.mktemp("doors")
    cache: dict[str, Door] = {}

    def get(case_id: str) -> Door:
        if case_id not in cache:
            cache[case_id] = _generate(CASEBOOK[case_id], root)
        return cache[case_id]

    return get


CASEBOOK = {case.id: case for case in CASES}
CASE_IDS = list(CASEBOOK)


# -- pin (a): the generated steps are the pass's plan -------------------------------


def door_statements(door: Door, dialect: str) -> list[str]:
    """Every DDL statement of ``0002``'s up files for ``dialect``, headers
    stripped and each online shape replaced by its plain twin. A rebuild
    stands for what it carries: the pass's statements for the ops on its
    table (or type) that no native statement of the migration runs — on
    SQLite mostly none, since the pass only warns there; pin (b) compares the
    rebuild itself. Data steps hold no DDL."""
    steps = [s for s in door.stems if not door.is_data(s)]
    split = {s: _split_rebuilds(door.statements(s, "up", dialect)) for s in steps}
    natives = [t for s in steps for t in _plain_twins(split[s][0])]
    ops = [op for plan in door.pass_plans(dialect) for op in plan]
    remaining = Counter(natives)
    out = list(natives)
    carried: set[int] = set()
    for step in steps:
        rebuilt = {r.subject for r in split[step][1]}
        for at, op in enumerate(ops):
            if at in carried or not _op_subjects(op) & rebuilt:
                continue
            carried.add(at)
            for statement in op["statements"]:
                if remaining[statement]:
                    remaining[statement] -= 1
                else:
                    out.append(statement)
    return out


def pass_statements(door: Door, dialect: str) -> list[str]:
    return [
        s for plan in door.pass_plans(dialect) for op in plan for s in op["statements"]
    ]


def assert_door_is_the_pass(door: Door, dialect: str) -> None:
    """Pin (a): the generated statements and the pass's, as multisets (the
    order is the generator's step assignment, ADR-0027, pinned by the
    generator tests)."""
    generated = Counter(door_statements(door, dialect))
    planned = Counter(pass_statements(door, dialect))
    if generated != planned:
        only_generated = "\n    ".join(sorted((generated - planned).elements()))
        only_planned = "\n    ".join(sorted((planned - generated).elements()))
        raise AssertionError(
            f"pin (a), {door.case.id} on {dialect}: the generated steps are not "
            "the reconciliation pass's plan\n"
            f"  generated only:\n    {only_generated}\n"
            f"  the pass only:\n    {only_planned}"
        )


# A rename hint and a type change in one plan: the pass cannot render it from
# two snapshots (nor from a live database: `connect(migrate_updates=True)`
# refuses with the same error), while the generator writes it.
_PASS_CANNOT_RENDER = {
    "F2-a-rename-and-a-type-change": (
        "finding (#538): the reconciliation pass refuses a rename hint plus a type "
        "change of the renamed column ('column \\'author.full_name\\' not found in "
        "IR context'); the generator renders it"
    ),
}


def _xfail_where_the_pass_cannot_render(case_ids: list[str]) -> list:
    return [
        pytest.param(
            case_id,
            marks=pytest.mark.xfail(
                strict=True, raises=ValueError, reason=_PASS_CANNOT_RENDER[case_id]
            ),
        )
        if case_id in _PASS_CANNOT_RENDER
        else case_id
        for case_id in case_ids
    ]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("case_id", _xfail_where_the_pass_cannot_render(CASE_IDS))
def test_pin_a_the_generated_steps_are_the_pass_plan(doors, case_id, dialect):
    assert_door_is_the_pass(doors(case_id), dialect)


def test_pin_a_names_the_case_the_dialect_and_both_statements(doors, tmp_path: Path):
    original = doors("A1-optional-column")
    copy = tmp_path / original.migration.name
    shutil.copytree(original.migration, copy)
    up = copy / "01_schema.up.postgres.sql"
    up.write_text(up.read_text().replace("varchar", "varchaR"))
    door = dataclasses.replace(original, migration=copy)

    with pytest.raises(AssertionError) as failure:
        assert_door_is_the_pass(door, "postgres")

    assert str(failure.value) == (
        "pin (a), A1-optional-column on postgres: the generated steps are not the "
        "reconciliation pass's plan\n"
        '  generated only:\n    ALTER TABLE "author" ADD COLUMN "bio" varchaR\n'
        '  the pass only:\n    ALTER TABLE "author" ADD COLUMN "bio" varchar'
    )


# -- the plain-twin normalizer, pinned first ----------------------------------------


def test_a_not_valid_add_normalizes_to_the_plain_add():
    assert normalize_online_shape(
        'ALTER TABLE "author" ADD CONSTRAINT "fk_author_team_id_team" FOREIGN KEY '
        '("team_id") REFERENCES "team" ("id") ON DELETE SET NULL NOT VALID'
    ) == (
        'ALTER TABLE "author" ADD CONSTRAINT "fk_author_team_id_team" FOREIGN KEY '
        '("team_id") REFERENCES "team" ("id") ON DELETE SET NULL'
    )
    # Inside the guarded column check, too.
    assert normalize_online_shape(
        'DO $$ BEGIN IF NOT EXISTS (SELECT 1) THEN ALTER TABLE "author" ADD '
        'CONSTRAINT "ck_author_mood" CHECK ("mood" IN (\'calm\')) NOT VALID; '
        "END IF; END $$"
    ) == (
        'DO $$ BEGIN IF NOT EXISTS (SELECT 1) THEN ALTER TABLE "author" ADD '
        'CONSTRAINT "ck_author_mood" CHECK ("mood" IN (\'calm\')); END IF; END $$'
    )


def test_concurrently_is_removed_for_the_plain_index_statement():
    assert (
        normalize_online_shape(
            'CREATE UNIQUE INDEX CONCURRENTLY "uq_author_email" ON "author" ("email")'
        )
        == 'CREATE UNIQUE INDEX IF NOT EXISTS "uq_author_email" ON "author" ("email")'
    )
    assert (
        normalize_online_shape(
            'CREATE INDEX CONCURRENTLY "idx_author_age" ON "author" ("age")'
        )
        == 'CREATE INDEX IF NOT EXISTS "idx_author_age" ON "author" ("age")'
    )
    assert (
        normalize_online_shape('DROP INDEX CONCURRENTLY IF EXISTS "uq_author_email"')
        == 'DROP INDEX IF EXISTS "uq_author_email"'
    )


def test_a_validate_normalizes_to_nothing():
    assert (
        normalize_online_shape(
            'ALTER TABLE "author" VALIDATE CONSTRAINT "ck_author_email_nonempty"'
        )
        == ""
    )


def test_the_staged_not_null_normalizes_to_nothing_and_set_not_null_stays():
    staged = [
        'ALTER TABLE "author" ADD CONSTRAINT "_ferro_notnull_author_slug" CHECK '
        '("slug" IS NOT NULL) NOT VALID',
        'ALTER TABLE "author" VALIDATE CONSTRAINT "_ferro_notnull_author_slug"',
        'ALTER TABLE "author" DROP CONSTRAINT "_ferro_notnull_author_slug"',
    ]
    assert [normalize_online_shape(s) for s in staged] == ["", "", ""]
    set_not_null = 'ALTER TABLE "author" ALTER COLUMN "slug" SET NOT NULL'
    assert normalize_online_shape(set_not_null) == set_not_null


def test_a_plain_statement_is_its_own_twin():
    plain = 'ALTER TABLE "author" ADD COLUMN "bio" varchar'
    assert normalize_online_shape(plain) == plain
