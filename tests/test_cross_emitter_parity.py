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

import ast
import asyncio
import contextlib
import dataclasses
import datetime
import decimal
import importlib
import io
import json
import re
import shutil
import sys
import uuid
import warnings
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
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
    ensure_resolved_modelset,
    reset_engine,
)
from ferro.migrations import drift as migrations_drift
from ferro.migrations import get_metadata
from tests._alembic_harness import autogen_opts, autogenerate, run_revision
from tests._casebook import CASES, Case
from tests.test_migrate_down import migration_dir
from tests.test_migrate_new import statements, write_config, write_models
from tests.test_migrate_up import Db, new

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
    assert [
        (report["kind"], report["blocks"])
        for report in plan["operations"][1]["reports"]
    ] == [({"ExtraEnumLabels": {"labels": ["legacy"]}}, False)]


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


@dataclass(frozen=True)
class Finding:
    """A pin this ticket found failing against merged behaviour. It is
    reported, not patched (#538 changes no production code): the pin runs
    and must fail exactly this way (``xfail(strict=True)``), so the fix
    turns it green and the strict xfail makes the fix remove this entry."""

    reason: str
    raises: type[BaseException]
    pins: frozenset[str]
    dialects: frozenset[str] = frozenset(DIALECTS)


FINDINGS: dict[str, Finding] = {}


def expect_finding(request, case_id: str, dialect: str, pin: str) -> None:
    finding = FINDINGS.get(case_id)
    if finding and pin in finding.pins and dialect in finding.dialects:
        request.applymarker(
            pytest.mark.xfail(strict=True, raises=finding.raises, reason=finding.reason)
        )


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("case_id", CASE_IDS)
def test_pin_a_the_generated_steps_are_the_pass_plan(request, doors, case_id, dialect):
    expect_finding(request, case_id, dialect, "a")
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


# -- which cases each online shape appears in -----------------------------------------
# Pins (b)–(d) run over the cases that carry their shape. The lists are
# explicit so a reader sees the coverage, and pinned against what the
# generator writes so they cannot go stale.

REBUILD_CASES = (
    "A2b-required-column-with-a-factory",
    "A3-required-column-without-a-default",
    "A4-drop-a-required-column",
    "A5-rename-a-column",
    "A6-change-a-type",
    "A7a-make-a-column-required",
    "A7b-make-a-column-optional",
    "A10-add-a-table-check",
    "A10-change-a-table-check",
    "A10-drop-a-table-check",
    "B3-rename-a-model",
    "C1-add-an-optional-foreign-key",
    "C1-add-a-required-foreign-key",
    "C2-drop-a-foreign-key-column",
    "C3-retarget-a-foreign-key",
    "D3-rename-a-label",
    "F2-a-rename-and-a-type-change",
    "F3-a-type-change-and-a-new-index",
)
"""SQLite table rebuilds, in an up or a down file. Postgres never rebuilds."""
INDEX_STEP_CASES = (
    "A3-required-column-without-a-default",
    "A9-add-a-unique",
    "A9-drop-a-unique",
    "F3-a-type-change-and-a-new-index",
)
"""Postgres index steps (``CONCURRENTLY``, no transaction)."""
LABEL_CASES = ("D1-add-a-label", "D1-add-a-label-and-a-column-of-its-type")
TYPE_CREATION_CASES = ("A1-optional-enum-column", "B1-new-model-with-a-new-type")
VALIDATE_CASES = (
    "A1-optional-checked-column",
    "A10-add-a-table-check",
    "A10-change-a-table-check",
    "C1-add-an-optional-foreign-key",
    "C3-retarget-a-foreign-key",
)
"""Postgres ``NOT VALID`` adds and their validate step."""


def _ddl_steps(door: Door) -> list[str]:
    return [stem for stem in door.stems if not door.is_data(stem)]


def _rebuilds(door: Door, dialect: str) -> list[tuple[str, str, Rebuild]]:
    """Every table rebuild of ``0002`` on ``dialect``: (step, direction,
    rebuild)."""
    return [
        (stem, direction, rebuild)
        for stem in _ddl_steps(door)
        for direction in ("up", "down")
        for rebuild in _split_rebuilds(door.statements(stem, direction, dialect))[1]
        if rebuild.table is not None
    ]


def _index_steps(door: Door) -> list[str]:
    """``0002``'s Postgres index steps: no-transaction, built concurrently."""
    return [
        stem
        for stem in _ddl_steps(door)
        if door.text(stem, "up", "postgres").startswith("-- ferro: no-transaction\n")
    ]


def _postgres_up(door: Door) -> list[str]:
    return [
        sql
        for stem in _ddl_steps(door)
        for sql in door.statements(stem, "up", "postgres")
    ]


def _shapes_written(door: Door) -> dict[str, bool]:
    postgres_up = _postgres_up(door)
    return {
        "rebuild": bool(_rebuilds(door, "sqlite")),
        "postgres_rebuild": bool(_rebuilds(door, "postgres")),
        "index_step": bool(_index_steps(door)),
        "label": any(" ADD VALUE IF NOT EXISTS " in sql for sql in postgres_up),
        "type_creation": any(
            sql.startswith("DO $$") and " CREATE TYPE " in sql for sql in postgres_up
        ),
        "validate": any(stem.endswith("_validate") for stem in door.stems),
    }


def test_each_online_shape_list_is_every_case_that_writes_it(doors):
    written: dict[str, list[str]] = {}
    for case_id in CASE_IDS:
        for shape, present in _shapes_written(doors(case_id)).items():
            if present:
                written.setdefault(shape, []).append(case_id)
    assert written == {
        "rebuild": list(REBUILD_CASES),
        "index_step": list(INDEX_STEP_CASES),
        "label": list(LABEL_CASES),
        "type_creation": list(TYPE_CREATION_CASES),
        "validate": list(VALIDATE_CASES),
    }


# -- pin (b): a rebuild's CREATE TABLE is the create pass's ---------------------------


def create_pass(shape: dict, dialect: str, table: str) -> list[str]:
    """What the create pass runs for ``table`` as ``shape`` declares it: its
    ``CREATE TABLE`` and its indexes."""
    return next(
        op["statements"]
        for op in _plan(_empty(shape), shape, dialect)
        if op["kind"] == "AddTable" and op["table"] == table
    )


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("case_id", REBUILD_CASES)
def test_pin_b_a_rebuild_creates_the_shape_its_step_leaves_as_the_create_pass(
    doors, case_id, dialect
):
    """The rebuild's ``CREATE TABLE "_ferro_new_<t>"`` is byte for byte the
    create pass's ``CREATE TABLE "<t>"`` for the shape the step leaves (the
    relaxed shape for an expand, the target for a contract, the parent going
    down), and the indexes it re-creates are the create pass's (ADR-0046) but
    the ones a later index step of the migration builds.
    Postgres alters every one of these in place: it writes no rebuild."""
    door = doors(case_id)
    if dialect == "postgres":
        assert _rebuilds(door, "postgres") == []
        return
    rebuilds = _rebuilds(door, "sqlite")
    assert rebuilds
    for stem, direction, rebuild in rebuilds:
        table = rebuild.table
        assert table is not None
        create, *indexes = create_pass(
            door.shape_left(stem, direction), "sqlite", table
        )
        # An index a later index step builds is that step's (ADR-0044).
        later = {f'"{s.split("_", 1)[1]}"' for s in _index_steps(door) if s > stem}
        indexes = [i for i in indexes if i.split(" ON ")[0].split()[-1] not in later]
        where = f"{case_id} {stem}.{direction}"
        assert (
            rebuild.create.replace(f'"_ferro_new_{table}"', f'"{table}"', 1) == create
        ), where
        assert rebuild.indexes == indexes, where


# -- pin (c): a concurrent build is the pass's statement but for one token ------------


def _changed_tokens(plain: str, online: str) -> list[tuple[list[str], list[str]]]:
    a, b = plain.split(), online.split()
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    return [
        (a[i1:i2], b[j1:j2])
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("case_id", INDEX_STEP_CASES)
def test_pin_c_a_concurrent_index_statement_is_the_pass_but_for_one_token(
    doors, case_id, dialect
):
    """ADR-0044: the index step's ``CREATE [UNIQUE] INDEX CONCURRENTLY`` is
    the pass's ``CREATE [UNIQUE] INDEX IF NOT EXISTS`` with that one token
    swapped, and its ``DROP INDEX CONCURRENTLY IF EXISTS`` the pass's ``DROP
    INDEX IF EXISTS`` plus it. SQLite's index step is the pass's statement
    itself."""
    door = doors(case_id)
    planned = pass_statements(door, dialect)
    if dialect == "sqlite":
        for stem in _index_steps(door):
            for sql in door.statements(stem, "up", "sqlite"):
                assert sql in planned, (case_id, sql)
        return
    online = [
        sql
        for stem in _index_steps(door)
        for sql in _plain_twins_kept_online(door.statements(stem, "up", "postgres"))
    ]
    assert online
    for sql in online:
        name = re.search(r'(?:CONCURRENTLY|EXISTS) ("[^"]+")', sql)[1]
        verb = sql.split(" ", 1)[0]
        plain = next(p for p in planned if p.startswith(verb) and f"{name} " in f"{p} ")
        expected = (
            [(["IF", "NOT", "EXISTS"], ["CONCURRENTLY"])]
            if verb == "CREATE"
            else [([], ["CONCURRENTLY"])]
        )
        assert _changed_tokens(plain, sql) == expected, (case_id, plain, sql)


def _plain_twins_kept_online(statements: list[str]) -> list[str]:
    """An index step's statements but the crash-leftover guard, as written."""
    kept = []
    for i, sql in enumerate(statements):
        following = statements[i + 1] if i + 1 < len(statements) else ""
        name = sql.removeprefix("DROP INDEX CONCURRENTLY IF EXISTS ")
        if (
            name != sql
            and following.startswith("CREATE")
            and f" {name} ON " in following
        ):
            continue
        kept.append(sql)
    return kept


# -- pin (d): validate, label addition and type creation go through one renderer -----


def _pass_statements_of(door: Door, dialect: str, kind: str) -> list[str]:
    return [
        sql
        for plan in door.pass_plans(dialect)
        for op in plan
        if op["kind"] == kind
        for sql in op["statements"]
    ]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("case_id", LABEL_CASES)
def test_pin_d_the_label_addition_is_the_pass_statement(doors, case_id, dialect):
    """The ``labels`` step runs the pass's ``ALTER TYPE … ADD VALUE IF NOT
    EXISTS`` (``render_pg_enum_add_value``, ADR-0011). SQLite stores labels
    as text: the step is not-applicable and the pass plans no addition."""
    door = doors(case_id)
    labels = next(stem for stem in door.stems if stem.endswith("_labels"))
    planned = _pass_statements_of(door, dialect, "AddEnumLabel")
    if dialect == "sqlite":
        assert door.text(labels, "up", "sqlite") == "-- ferro: not-applicable\n"
        assert planned == []
        return
    assert door.statements(labels, "up", "postgres") == planned
    assert planned


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("case_id", TYPE_CREATION_CASES)
def test_pin_d_the_type_creation_is_the_pass_statement(doors, case_id, dialect):
    """A type the migration introduces is created by the pass's guarded
    ``CREATE TYPE`` (``render_pg_enum_create_type``, ADR-0041), never a
    second rendering. SQLite has no enum types: neither side creates one."""
    door = doors(case_id)
    created = [
        sql
        for stem in _ddl_steps(door)
        for sql in door.statements(stem, "up", dialect)
        if " CREATE TYPE " in sql
    ]
    planned = _pass_statements_of(door, dialect, "CreateEnumType")
    assert created == planned
    assert bool(planned) is (dialect == "postgres")


# -- the database pins: a project, a second database, the live schema ----------------


@dataclass(frozen=True)
class Project:
    root: Path
    pkg: str

    def register(self, body: str) -> dict:
        """Declare ``body``'s models in this process; returns their envelope."""
        write_models(self.root, self.pkg, body)
        importlib.import_module(f"{self.pkg}.models")
        return ensure_resolved_modelset()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request):
    """A ferro project under ``tmp_path``, the working directory; its package
    is forgotten afterwards."""
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.delenv("FERRO_CONFIG", raising=False)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, "path", [str(root), *sys.path])
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    pkg = "ferro_p538_" + uuid.uuid4().hex[:12]
    yield Project(root, pkg)
    for name in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
        del sys.modules[name]
    _rewind_registry()


@pytest.fixture
def second_db(tmp_path: Path, db_backend: str, postgres_base_url: str | None):
    """A second, empty database of the same backend: ``(url, schema)``."""
    if db_backend == "sqlite":
        yield f"sqlite:{tmp_path / 'second.db'}?mode=rwc", None
        return
    import psycopg

    from tests.db_backends import build_postgres_test_url

    assert postgres_base_url is not None
    schema = f"p538_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(postgres_base_url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield build_postgres_test_url(postgres_base_url, schema), schema
    finally:
        reset_engine()
        with psycopg.connect(postgres_base_url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _cli(*argv: str) -> int:
    from ferro.cli import main

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        return main(list(argv))


def migrate_through(
    project: Project, case: Case, url: str, backend: str
) -> Path | None:
    """``0001`` creates ``case.before`` and is applied; ``0002`` is the edit,
    each backfill's ``todo(...)`` written as ``None`` (the tables are empty,
    so no row asks for one), and is applied. Returns ``0002``, or ``None``
    where the edit changes nothing on ``backend`` (row security or an enum
    type on SQLite: ``new`` writes no migration)."""
    write_config(project.root, project.pkg, f'["{backend}"]')
    write_models(project.root, project.pkg, case.before)
    new("create")
    assert _cli("migrate", "up", "--url", url) == 0
    write_models(project.root, project.pkg, case.after)
    new("edit")
    if not list((project.root / "migrations").glob("0002_*")):
        return None
    migration = migration_dir(project.root, 2)
    for scaffold in migration.glob("*.py"):
        scaffold.write_text(_TODO.sub("None", scaffold.read_text()))
    assert _cli("migrate", "up", "--url", url) == 0
    return migration


async def _read_live(
    url: str, declared: dict, extra: frozenset[str] = frozenset()
) -> tuple[str, str]:
    """The live read planned against ``declared``, with the live tables in
    ``extra`` (the tables a change drops) beside it."""
    name = f"p538_{uuid.uuid4().hex}"
    await connect(url, name=name)
    try:
        return await _core._live_schema_ir(
            name, json.dumps(declared), json.dumps(sorted(extra))
        )
    finally:
        await _core._disconnect(name)


def _canonical(value):
    """``value`` with every list of objects sorted: the live schema's
    catalog order (which index was built first) is not schema."""
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [_canonical(v) for v in value]
        if all(isinstance(v, dict) for v in items):
            return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
        return items
    return value


def live_schema(url: str, declared: dict, extra: frozenset[str] = frozenset()) -> dict:
    """The live schema and facts as the reconciliation pass reads them."""
    live, facts = asyncio.run(_read_live(url, declared, extra))
    return _canonical({"schema": json.loads(live), "facts": json.loads(facts)})


# -- AGENTS.md describes the emitters that ship ----------------------------------------

REPO = Path(__file__).resolve().parents[1]


def test_agents_md_names_the_migrations_door_its_pins_and_the_one_bridge_item():
    agents = (REPO / "AGENTS.md").read_text()
    i1 = agents[agents.index("## I-1:") : agents.index("## I-2:")]
    emitters = i1[: i1.index("For a single model")]
    assert "`src/ferro/migrations/`" in emitters
    assert "`crates/ferro-migrate/src/generate/`" in emitters
    for pin in ("(a)", "(b)", "(c)", "(d)", "(e)", "(f)"):
        assert f"**{pin}**" in i1, pin
    assert "translates the one planner's ops" in i1
    # Items 11–17 are one item now: no per-family comparator, no slot rule.
    assert "\n12. " not in i1
    for gone in (
        "_plan_check_addition",
        "_plan_check_rebuild",
        "_plan_check_drop",
        "FerroRowSecurityOp",
        "FerroEnumTypeIntroducedOp",
    ):
        assert gone not in agents, gone
    assert "I-12" not in agents
    assert "## I-13:" in agents
    assert "every refusal" in agents[agents.index("## I-6:") : agents.index("## I-7:")]


def test_no_test_or_doc_relies_on_a_comparator_slot():
    """The comparators are gone (#533): nothing outside the ADRs (history)
    still describes Alembic ``priority=LAST`` / ``FIRST`` slots."""
    offenders = [
        str(path.relative_to(REPO))
        for root in ("tests", "docs")
        for path in (REPO / root).rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".md"}
        and "adr" not in path.parts
        and path.name != Path(__file__).name
        and re.search(r"priority=(LAST|FIRST)", path.read_text())
    ]
    assert offenders == []


def auto_migrate(url: str, name: str | None = None) -> None:
    """``connect(url, auto_migrate=True)``. SQLite's one warning for a table
    declaring row security (ADR-0014) is expected here, not reported."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=r"ferro auto-migrate: Table '\w+' declares __ferro_rls__"
        )
        asyncio.run(connect(url, name=name, auto_migrate=True))


def _tables(envelope: dict) -> set[str]:
    return {m["table_name"] for m in envelope["payload"]["models"]}


def _empty_autogenerate(url: str, base: str | None, schema: str | None) -> None:
    upgrade, downgrade = autogenerate(url, base, schema)
    assert "op." not in upgrade, upgrade
    assert "op." not in downgrade, downgrade


# -- pin (d), validate: the validate step is the pass's VALIDATE ----------------------


@pytest.mark.parametrize("case_id", VALIDATE_CASES)
def test_pin_d_the_validate_step_is_the_pass_statement(
    project, case_id, db_url, db_backend
):
    """Between the ``NOT VALID`` add and the validate step the database holds
    an unvalidated constraint; the pass, reading it live, plans exactly the
    validate step's ``VALIDATE CONSTRAINT`` (``render_validate_constraint``,
    ADR-0043)."""
    if db_backend == "sqlite":
        pytest.skip(
            "SQLite adds every foreign key and check validated: the generator "
            "writes no validate step there and the pass plans none"
        )
    migration = migrate_through(project, CASEBOOK[case_id], db_url, db_backend)
    assert _cli("migrate", "down", "--yes", "--to", "0002:01", "--url", db_url) == 0
    target = json.loads((migration / "ir.json").read_text())

    live, facts = asyncio.run(_read_live(db_url, target))
    plan = json.loads(
        _core._plan_from_ir(
            live, json.dumps(target), "postgres", DESTRUCTIVE, True, facts
        )
    )["operations"]

    assert {op["kind"] for op in plan} == {"ValidateConstraint"}
    assert [sql for op in plan for sql in op["statements"]] == statements(
        migration / "02_validate.up.postgres.sql"
    )


# -- pin (e): a migrated database is an auto-migrated one -----------------------------


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_pin_e_a_migrated_database_is_the_auto_migrated_one(
    project, second_db, case_id, db_url, db_backend, postgres_base_url, db_schema_name
):
    """The chain ``0001`` → ``0002`` leaves no drift; a second database
    ``connect(auto_migrate=True)`` builds from the same models has the same
    live schema, facts included; Alembic autogenerate writes nothing against
    the auto-migrated one. Against the migrated one it refuses, by design:
    a database the tracking table marks is ``ferro migrate new``'s, and the
    bridge reads the very live schema just shown equal, so it has nothing
    to say there either."""
    case = CASEBOOK[case_id]
    migrate_through(project, case, db_url, db_backend)

    report = asyncio.run(migrations_drift(url=db_url))
    assert (report.refusal, report.lines) == (None, []), report
    second, second_schema = second_db
    after = project.register(case.after)
    auto_migrate(second)

    assert live_schema(db_url, after) == live_schema(second, after)
    _empty_autogenerate(second, postgres_base_url, second_schema)
    with pytest.raises(RuntimeError, match="tracked by ferro migrations"):
        autogenerate(db_url, postgres_base_url, db_schema_name)


# -- pin (f): the bridge's revision runs the pass's DDL -------------------------------


def _executed_literals(code: str) -> list[str]:
    """Every statement a revision body runs as ``op.execute(sa.DDL('…'))``."""
    tree = ast.parse("def upgrade():\n" + code)
    return [
        node.args[0].value.replace("%%", "%")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "DDL"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]


def _plan_live(live: str, envelope: dict, dialect: str, facts: str) -> list[dict]:
    return json.loads(
        _core._plan_from_ir(
            live, json.dumps(envelope), dialect, DESTRUCTIVE, True, facts
        )
    )["operations"]


def _run_statements(
    url: str, backend: str, base: str | None, schema: str | None, sql: list[str]
) -> None:
    db = Db(url, backend, base, schema)
    for statement in sql:
        db.execute(statement)


def _pass_declines(url: str) -> bool:
    """Whether the reconciliation pass, run for real, declines the change
    and points at ``ferro migrate new``: it refuses so, or it warns so and
    leaves the table as it is (ADR-0014's SQLite posture)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            asyncio.run(
                connect(
                    url,
                    name="p538_pass_run",
                    migrate_updates=True,
                    migrate_destructive=True,
                )
            )
        except Exception as refusal:  # noqa: BLE001 - the refusal is the answer
            return "ferro migrate new" in str(refusal)
    return any("ferro migrate new" in str(w.message) for w in caught)


ROWS_HELD = {
    "D3-rename-a-label": (
        "INSERT INTO \"enmorder\" (\"status\") VALUES ('canceled')",
        'SELECT "status" FROM "enmorder"',
    ),
}
"""Rows pin (f) seeds into both databases where a case is only live while
rows hold something: a label rename on SQLite is live while a row holds the
old label (ADR-0032), and inert, on both doors' word, otherwise."""


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_pin_f_the_bridge_revision_runs_the_pass_ddl(
    request,
    project,
    second_db,
    case_id,
    db_url,
    db_backend,
    postgres_base_url,
    db_schema_name,
):
    """Two databases at ``case.before``. One runs the revision Alembic
    autogenerate writes for ``case.after``; the other runs the statements the
    reconciliation pass plans from the same live database (every table of
    either modelset, destructive changes on). Every statement the revision
    runs as written is one of the pass's, and the two databases end with the
    same live schema, facts included.

    Where the pass carries no change out, neither does the bridge: a value
    existing rows lack is the plain op marked ``# ferro: data-dependent``
    (the pass refuses naming ``ferro migrate new``), and a change SQLite can
    only make by a table rebuild is refused naming ``ferro migrate new``
    (the pass declines it too, by a warning or a refusal naming the same
    command)."""
    expect_finding(request, case_id, db_backend, "f")
    case = CASEBOOK[case_id]
    second, second_schema = second_db
    write_config(project.root, project.pkg, f'["{db_backend}"]')
    before = project.register(case.before)
    auto_migrate(db_url)
    auto_migrate(second, name="p538_pass")
    seed, rows_of = ROWS_HELD.get(case_id, (None, None))
    databases = [
        Db(db_url, db_backend, postgres_base_url, db_schema_name),
        Db(second, db_backend, postgres_base_url, second_schema),
    ]
    if seed:
        for db in databases:
            db.execute(seed)
    held = [db.rows(rows_of) for db in databases] if rows_of else None
    after = project.register(case.after)
    dropped = frozenset(_tables(before))
    live, facts = asyncio.run(_read_live(db_url, after, dropped))
    try:
        planned = _plan_live(live, after, db_backend, facts)
    except ValueError as refusal:
        if "ferro migrate new" not in str(refusal):
            raise
        planned = None

    try:
        upgrade, _ = autogenerate(db_url, postgres_base_url, db_schema_name)
    except RuntimeError as refusal:
        assert "autogenerate refused" in str(refusal), refusal
        assert "`ferro migrate new`" in str(refusal), refusal
        assert _pass_declines(second), str(refusal)
        if rows_of:
            # Neither door changed a row: the relabel is the generated
            # migration's.
            assert [db.rows(rows_of) for db in databases] == held
        return
    if planned is None:
        assert "# ferro: data-dependent" in upgrade, upgrade
        return

    statements_planned = [sql for op in planned for sql in op["statements"]]
    for statement in _executed_literals(upgrade):
        assert statement in statements_planned, (statement, statements_planned)
    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    _run_statements(
        second, db_backend, postgres_base_url, second_schema, statements_planned
    )
    assert live_schema(db_url, after, dropped) == live_schema(second, after, dropped)
