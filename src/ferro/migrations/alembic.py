"""The Alembic bridge (ADR-0041): ferro's models as Alembic metadata, and one
comparator that writes what the one planner decides.

```python
# env.py
from ferro.migrations import ferro_options, get_metadata

context.configure(connection=connection, target_metadata=get_metadata(), **ferro_options())
```

``alembic revision --autogenerate`` reads the live database through the
reconciliation pass's converter (``_core._live_schema_ir``), plans it
against the models with destructive changes on (``_core._plan_from_ir``) and
translates the planner's ops (:mod:`ferro.migrations.translate`); its
``downgrade()`` is the planner run back to the live database
(``_core._plan_reverse_from_ir``). An empty revision and "no drift" are the
same statement. ``ferro_options()`` hides ferro's tables and both tracking
tables from Alembic's own comparator, so every op on a ferro table is the
planner's; a project's own SQLAlchemy tables keep Alembic's comparison.
"""

import asyncio
import concurrent.futures
import json
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Literal, TypeVar
from urllib.parse import quote

try:
    import sqlalchemy as sa
except ImportError:
    sa = None

if TYPE_CHECKING:
    from alembic.autogenerate.api import AutogenContext

from .. import _core
from .._annotation_utils import _VARCHAR_RE
from .._core import (
    _ddl_fk_name,
    _render_check_body,
    _render_table_check_body,
    _resolve_storage_type,
)

#: SQLAlchemy ``naming_convention`` mirroring the Rust emitter's names. IR-backed
#: metadata names every artifact explicitly; this convention covers any
#: SQLAlchemy-generated fallback names during autogenerate.
_FERRO_NAMING_CONVENTION = {
    "ix": "idx_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
}


def _naming_metadata() -> "sa.MetaData":
    """An empty ``MetaData`` carrying ferro's naming convention."""
    if sa is None:
        raise ImportError(
            "SQLAlchemy is required to use the alembic bridge. "
            "Install it via 'pip install ferro-orm[alembic]'."
        )
    return sa.MetaData(naming_convention=_FERRO_NAMING_CONVENTION)


@dataclass(frozen=True)
class _FerroMetadata:
    """What ``get_metadata()`` marks its ``MetaData`` with
    (``metadata.info["ferro"]``): the SchemaIR modelset its tables render."""

    envelope: Dict[str, Any]

    @property
    def tables(self) -> list[str]:
        """The models' tables and every table a rename hint names as the old
        one: the tables the planner reads live and Alembic is kept off."""
        names = set()
        for model in self.envelope["payload"]["models"]:
            names.add(model["table_name"])
            if model.get("renamed_from"):
                names.add(model["renamed_from"])
        return sorted(names)


def _declared_modelset(database: "str | None") -> Dict[str, Any]:
    """The modelset ``get_metadata`` renders: the configured database's
    models when the project has a ferro configuration, else every registered
    model."""
    from ..settings import FerroSettings

    settings = FerroSettings()
    if not settings.databases and database is None:
        from .. import ensure_resolved_modelset

        return ensure_resolved_modelset()
    from .generate import declared_modelset

    # With no configuration, naming a database is refused by `database()`
    # itself, with the directories it searched.
    return declared_modelset(settings.database(database))


def get_metadata(database: "str | None" = None) -> "sa.MetaData":
    """
    Generate a SQLAlchemy MetaData object representing the project's Ferro
    models, for alembic's env.py together with :func:`ferro_options`.

    With a ferro configuration (``ferro.toml`` or ``[tool.ferro]``, read
    through ``FerroSettings``) it imports the configured database's
    ``models`` modules and returns only that database's tables; name the
    database with ``database=`` when several are configured. A registered
    model no configured database claims is refused by name. With no
    configuration it renders every registered model.

    Enum columns are mapped to named ``sqlalchemy.Enum`` types so PostgreSQL
    autogenerate and DDL compilation succeed (anonymous enums are rejected).
    When the field annotation is a Python ``enum.Enum`` subclass, the database
    type name defaults to the enum class name in lowercase; otherwise the
    column name is used as the type name.

    **Column nullability:** ``Column.nullable`` follows :class:`~ferro.base.FerroField`
    / :class:`~ferro.base.ForeignKey` ``nullable`` when set to a boolean (force
    NULL / NOT NULL). The default ``nullable='infer'`` uses whether the Python
    annotation allows ``None`` (after unwrapping ``Annotated``). Shadow ``*_id``
    columns infer from the **forward relation** field's annotation, not from the
    synthetic ``*_id`` field. Primary key columns are always ``nullable=False``.
    Pydantic "required" and JSON-schema defaults do not change inferred nullability.
    """
    metadata = _naming_metadata()
    envelope = _declared_modelset(database)
    for model_ir in envelope.get("payload", {}).get("models", []):
        if isinstance(model_ir, dict):
            _build_sa_table_from_ir(metadata, model_ir)
    metadata.info["ferro"] = _FerroMetadata(envelope)
    return metadata


def _build_sa_table_from_ir(metadata: "sa.MetaData", model_ir: Dict[str, Any]) -> None:
    table_name = model_ir.get("table_name")
    if not isinstance(table_name, str) or not table_name:
        return

    columns = []
    columns_by_name: dict[str, Any] = {}
    for col in model_ir.get("columns") or []:
        if not isinstance(col, dict):
            continue
        col_name = col.get("name")
        if not isinstance(col_name, str) or not col_name:
            continue
        sa_type = _sa_type_from_ir_column(col_name, col)
        # No unique=/index= column flags: single-column uniques and indexes are
        # explicit named artifacts below (the IR `uniques[]`/`indexes[]` carry
        # the shared `uq_`/`idx_` names), matching the Rust emitter's shapes.
        kwargs = {
            "primary_key": bool(col.get("primary_key", False)),
            "nullable": bool(col.get("nullable", True))
            if not bool(col.get("primary_key", False))
            else False,
        }
        columns.append(sa.Column(col_name, sa_type, **kwargs))
        columns_by_name[col_name] = columns[-1]

    table_args: list[Any] = list(columns)

    for check in model_ir.get("checks") or []:
        if not isinstance(check, dict):
            continue
        name = check.get("name")
        column = check.get("column")
        values = check.get("values")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(column, str) or not column:
            continue
        if not isinstance(values, list) or not values:
            continue
        sqltext = _render_check_body(column, values)
        table_args.append(sa.CheckConstraint(sqltext, name=name))

    # Table checks (ADR-0012): the same named CHECKs the Rust emitter folds
    # into CREATE TABLE, with the body rendered by the shared Rust renderer
    # over the IR predicate — never a second body language (I-1).
    for table_check in model_ir.get("table_checks") or []:
        if not isinstance(table_check, dict):
            continue
        name = table_check.get("name")
        predicate = table_check.get("predicate")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(predicate, dict) or not predicate:
            continue
        sqltext = _render_table_check_body(json.dumps(predicate))
        table_args.append(sa.CheckConstraint(sqltext, name=name))

    # Every `uniques[]` entry — single-column included — is a standalone named
    # unique index, the one shape the Rust emitter, the SQLite ALTER path, and
    # reflection all share (FF-B B4/D1). A UniqueConstraint would reflect as a
    # different structural shape and produce phantom diffs.
    unique_index_args: list[tuple[str, list[str]]] = []
    for unique in model_ir.get("uniques") or []:
        if not isinstance(unique, dict):
            continue
        cols = unique.get("columns")
        name = unique.get("name")
        if not isinstance(cols, list) or len(cols) < 1:
            continue
        if not all(isinstance(c, str) and c for c in cols):
            continue
        if not isinstance(name, str) or not name:
            continue
        unique_index_args.append((name, cols))

    table = sa.Table(table_name, metadata, *table_args)

    for fk in model_ir.get("foreign_keys") or []:
        if not isinstance(fk, dict):
            continue
        col_name = fk.get("column")
        to_table = fk.get("to_table")
        to_column = fk.get("to_column") or "id"
        if not isinstance(col_name, str) or col_name not in columns_by_name:
            continue
        if not isinstance(to_table, str) or not to_table:
            continue
        if not isinstance(to_column, str) or not to_column:
            continue
        on_delete = fk.get("on_delete")
        fk_ir_name = fk.get("name")
        constraint_name = (
            fk_ir_name
            if isinstance(fk_ir_name, str) and fk_ir_name
            else _ddl_fk_name(table_name, col_name, to_table)
        )
        table.append_constraint(
            sa.ForeignKeyConstraint(
                [col_name],
                [f"{to_table}.{to_column}"],
                name=constraint_name,
                ondelete=on_delete if isinstance(on_delete, str) else None,
            )
        )

    for name, cols in unique_index_args:
        if not all(c in table.columns for c in cols):
            continue
        sa.Index(name, *(table.columns[c] for c in cols), unique=True)

    for index in model_ir.get("indexes") or []:
        if not isinstance(index, dict):
            continue
        cols = index.get("columns")
        name = index.get("name")
        unique = bool(index.get("unique", False))
        if not isinstance(cols, list) or not cols:
            continue
        if not isinstance(name, str) or not name:
            continue
        if not all(isinstance(c, str) and c in table.columns for c in cols):
            continue
        sa.Index(name, *(table.columns[c] for c in cols), unique=unique)


def _sa_type_from_ir_column(col_name: str, col: Dict[str, Any]) -> "sa.types.TypeEngine":
    """Mechanical consumer of the shared derived-type decision table (FF-B B2).

    The storage decision — explicit ``db_type`` wins, then enum values select
    native enum storage, then the ``(logical_type, format)`` cascade — is made
    by ``ferro_ddl_lowering::resolve_column_storage`` over FFI; this function
    only maps the resolved token/enum onto SQLAlchemy types. The dialect is
    fixed to ``"postgres"`` (the richer vocabulary): SQLAlchemy applies its own
    per-dialect lowering, which matches the Rust emitter's dialect splits
    (pinned by tests/test_db_type_cross_emitter_parity.py).
    """
    # SchemaColumn's non-Option fields must be present for deserialization;
    # defaults cover callers that pass minimal column dicts.
    column_ir = {
        "logical_type": "unknown",
        "nullable": True,
        "primary_key": False,
        "autoincrement": False,
        "unique": False,
        "index": False,
        "default": None,
        "format": None,
        "postgres_native_enum": False,
        **col,
        "name": col_name,
    }
    resolved = json.loads(_resolve_storage_type(json.dumps(column_ir), "postgres"))
    if resolved["kind"] == "pg_enum":
        return sa.Enum(*resolved["labels"], name=resolved["name"])
    mapped = _db_type_to_sa_type(resolved["token"])
    if mapped is None:
        raise RuntimeError(
            f"resolve_column_storage returned unmapped token {resolved['token']!r} "
            f"for column {col_name!r} — extend _db_type_to_sa_type (see AGENTS.md I-1)"
        )
    return mapped


#: SQLAlchemy RENDERING of the shared ``db_type`` token vocabulary. This is
#: not a second decision table: which token a column gets is decided by
#: ``ferro_ddl_lowering::resolve_column_storage``/``canonical_to_db_type_token``
#: (consumed over FFI in ``_sa_type_from_ir_column``); this function only maps
#: each token 1:1 onto an SA type. Exhaustiveness over the full vocabulary is
#: pinned by tests/test_db_type_cross_emitter_parity.py. See AGENTS.md § I-1.
def _db_type_to_sa_type(token: str) -> "sa.types.TypeEngine | None":
    """Return the SA type for a canonical ``db_type`` token, or ``None`` if
    unrecognized. Validation at class-definition time (see metaclass) means an
    unrecognized token reaching here is a programming error."""
    if sa is None:
        return None

    if token == "text":
        return sa.Text()
    if token == "smallint":
        return sa.SmallInteger()
    if token == "int":
        return sa.Integer()
    if token == "bigint":
        return sa.BigInteger()
    if token == "uuid":
        return sa.Uuid() if hasattr(sa, "Uuid") else sa.String(36)
    if token == "timestamp":
        return sa.DateTime(timezone=False)
    if token == "timestamptz":
        return sa.DateTime(timezone=True)
    if token == "date":
        return sa.Date()
    if token == "time":
        return sa.Time()
    if token == "boolean":
        return sa.Boolean()
    if token == "double":
        return sa.Double()
    if token == "numeric":
        return sa.Numeric()
    if token == "json":
        return sa.JSON()
    if token == "jsonb":
        # One SA type per token, SQLAlchemy carrying the dialect split
        # (ADR-0004): JSONB on Postgres, plain JSON on SQLite — mirroring the
        # Rust emitter's token-seam lowering. A bare postgresql.JSONB() would
        # fail to compile on SQLite.
        from sqlalchemy.dialects import postgresql

        return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    if token in {"bytea", "blob"}:
        return sa.LargeBinary()
    if token == "varchar":
        return sa.String()

    match = _VARCHAR_RE.match(token)
    if match is not None:
        return sa.String(length=int(match.group(1)))
    if token.startswith("char(") and token.endswith(")"):
        try:
            return sa.CHAR(int(token[5:-1]))
        except ValueError:
            return None
    return None


def render_item(
    type_: str, obj: Any, autogen_context: "AutogenContext"
) -> "str | Literal[False]":
    """Alembic ``render_item`` hook for ferro's bridge, carried by
    :func:`ferro_options` (which ``env.py`` passes to ``context.configure``).

    It renders one thing Alembic cannot: a native Postgres enum type the
    revision must **not** create. Every enum type a revision needs is
    created by the planner's own ``CREATE TYPE`` (or already exists), so a
    ``create_table`` / ``add_column`` column of it is written
    ``postgresql.ENUM(..., create_type=False)`` — or SQLAlchemy would issue
    ``CREATE TYPE`` again and the upgrade would fail with ``DuplicateObject``
    (#443). ``repr()`` of that type silently drops the flag, and Alembic's
    default type renderer is that ``repr``; this hook renders the flag and
    adds the ``from sqlalchemy.dialects import postgresql`` import.

    Everything else returns ``False`` so Alembic's own renderers keep their
    say. The signature is Alembic's ``RenderItemFn`` exactly —
    ``(str, Any, AutogenContext) -> str | Literal[False]`` (#446).
    """
    if type_ != "type" or sa is None:
        return False
    from sqlalchemy.dialects import postgresql

    if not isinstance(obj, postgresql.ENUM) or obj.create_type:
        return False
    if autogen_context.imports is not None:
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")
    labels = ", ".join(repr(label) for label in obj.enums)
    return f"postgresql.ENUM({labels}, name={obj.name!r}, create_type=False)"


# ---------------------------------------------------------------------------
# ferro_options(): the one wired line (ADR-0041).
# ---------------------------------------------------------------------------

_ferro_render_item = render_item

_OPTIONS_LINE = (
    "context.configure(..., target_metadata=get_metadata(), **ferro_options())"
)

_IncludeObject = Callable[[Any, "str | None", str, bool, Any], bool]
_RenderItem = Callable[[str, Any, "AutogenContext"], "str | Literal[False]"]


class _FerroObjectFilter:
    """Alembic's ``include_object`` hook from :func:`ferro_options`: hides
    ferro's tables (and everything on them) and both tracking tables from
    Alembic's own comparator, then asks the project's own filter, if any.

    The tables are the ones the comparator is about to plan, set by it
    (``hide``) before Alembic compares a single table."""

    def __init__(self, wrapped: "_IncludeObject | None") -> None:
        self.wrapped = wrapped
        self.hidden: frozenset[str] = frozenset(_core._tracking_table_names())

    def hide(self, tables: list[str]) -> None:
        self.hidden = frozenset(tables) | frozenset(_core._tracking_table_names())

    def __call__(
        self, obj: Any, name: "str | None", type_: str, reflected: bool, compare_to: Any
    ) -> bool:
        table = (
            name
            if type_ == "table"
            else getattr(getattr(obj, "table", None), "name", None)
        )
        if table in self.hidden:
            return False
        if self.wrapped is not None:
            return self.wrapped(obj, name, type_, reflected, compare_to)
        return True


def ferro_options(
    *,
    include_object: "_IncludeObject | None" = None,
    render_item: "_RenderItem | None" = None,
) -> dict[str, Any]:
    """The options ``env.py`` passes to ``context.configure`` beside
    ``get_metadata()``::

        context.configure(
            connection=connection,
            target_metadata=get_metadata(),
            **ferro_options(),
        )

    ``include_object`` keeps Alembic's own comparator off ferro's tables and
    both tracking tables, so every op on a ferro table is the planner's;
    ``render_item`` writes a native enum type the revision must not create
    (``postgresql.ENUM(..., create_type=False)``). A project with its own
    hooks passes them here: ferro's filter asks the project's for every
    object it does not hide, and ferro's renderer falls through to the
    project's on ``False``. Autogenerate over ``get_metadata()`` without
    these options is refused, naming this line.
    """

    def rendered(
        type_: str, obj: Any, autogen_context: "AutogenContext"
    ) -> "str | Literal[False]":
        result = _ferro_render_item(type_, obj, autogen_context)
        if result is False and render_item is not None:
            return render_item(type_, obj, autogen_context)
        return result

    return {
        "include_object": _FerroObjectFilter(include_object),
        "render_item": rendered,
    }


# ---------------------------------------------------------------------------
# The live database, read the way the reconciliation pass reads it.
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


def _run_to_completion(make: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
    """Run a coroutine to completion from Alembic's synchronous comparator:
    on this thread when no event loop runs (a plain ``env.py``), on a
    dedicated thread with its own loop when one does (an async ``env.py``
    calling ``connection.run_sync``, where ``asyncio.run`` cannot nest)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(make())).result()


def _ferro_url(connection: Any) -> str:
    """The URL ferro connects to for the database ``connection`` is on, scoped
    to the schema Alembic compares (Postgres: its ``current_schema()``)."""
    url = connection.engine.url
    backend = url.get_backend_name()
    if backend == "postgresql":
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar()
        rendered = url.set(drivername="postgresql").render_as_string(
            hide_password=False
        )
        joiner = "&" if "?" in rendered else "?"
        return f"{rendered}{joiner}ferro_search_path={quote(str(schema))}"
    if backend == "sqlite":
        path = url.database
        if not path or path == ":memory:":
            raise RuntimeError(
                "ferro: autogenerate reads the live database through its own "
                "connection, and an in-memory SQLite database is a different "
                "database on every connection; autogenerate against a SQLite file"
            )
        return f"sqlite:{path}?mode=ro"
    raise RuntimeError(
        f"ferro: the Alembic bridge plans for Postgres and SQLite; this connection "
        f"is {backend}"
    )


@dataclass(frozen=True)
class _LiveDatabase:
    schema_ir: str
    facts: str
    tracking_tables: list[Any]


def _read_live(connection: Any, tables: list[str]) -> _LiveDatabase:
    """The live database behind ``connection`` read into the planner's input
    (``_live_schema_ir``) and the tracking tables governing its schema, over
    a private ferro connection closed afterwards."""
    url = _ferro_url(connection)

    async def read() -> _LiveDatabase:
        from .. import connect

        name = f"_ferro_alembic_{uuid.uuid4().hex}"
        await connect(url, name=name)
        try:
            schema_ir, facts = await _core._live_schema_ir(name, json.dumps(tables))
            tracking = json.loads(await _core._tracking_tables_for(name, None))
        finally:
            await _core._disconnect(name)
        return _LiveDatabase(schema_ir, facts, tracking)

    return _run_to_completion(read)


# ---------------------------------------------------------------------------
# The comparator: one decider, the planner (ADR-0041).
# ---------------------------------------------------------------------------

_DESTRUCTIVE = json.dumps({"destructive": True})
_DIALECTS = {"postgresql": "postgres", "sqlite": "sqlite"}


def _ferro_metadata(metadata: Any) -> "_FerroMetadata | None":
    candidates = metadata if isinstance(metadata, (list, tuple)) else [metadata]
    for candidate in candidates:
        info = getattr(candidate, "info", None) or {}
        if isinstance(info.get("ferro"), _FerroMetadata):
            return info["ferro"]
    return None


def _refuse(text: str) -> RuntimeError:
    return RuntimeError(f"ferro: autogenerate refused: {text}")


def _subject(op: Dict[str, Any]) -> str:
    table = op.get("table") or op.get("type_name") or op.get("new") or ""
    return f"{table}.{op['column']}" if op.get("column") else table


def _upgrade_plan(
    live: _LiveDatabase, declared: Dict[str, Any], dialect: str
) -> Dict[str, Any]:
    """The planner's upgrade, every op checked against the generator's
    verdicts: a primary-key change and a SQLite table rebuild are refused
    (no Alembic op writes them), an op the pass has no statement for is
    refused with the renderer's reason, and a change needing values of
    existing rows is marked."""
    declared_json = json.dumps(declared)
    plan = json.loads(
        _core._plan_from_ir(
            live.schema_ir, declared_json, dialect, _DESTRUCTIVE, True, live.facts
        )
    )
    for warning in plan["always_warnings"]:
        if warning.startswith("rename hint refused"):
            raise _refuse(warning)
    # An op the pass renders to nothing at all (a SQLite type change whose
    # storage is the same) is one the pass does not run: neither does the
    # revision.
    operations = plan["operations"] = [
        op for op in plan["operations"] if op["statements"] or op["warnings"]
    ]
    verdicts = json.loads(
        _core._plan_step_verdicts(
            live.schema_ir, declared_json, dialect, "up", json.dumps(operations)
        )
    )
    for op, verdict in zip(operations, verdicts):
        if verdict["refusal"] is not None:
            raise _refuse(verdict["refusal"])
        if verdict["needs"] == "rebuild":
            raise _refuse(
                f"{op['kind']} on {_subject(op)} needs a SQLite table rebuild, which an "
                f"Alembic revision cannot write (batch mode has no foreign-key pragma "
                f"handling, so the drop cascades into ON DELETE CASCADE children). "
                f"Write this change as an in-house migration: `ferro migrate new`"
            )
        if not op["statements"] and op["warnings"] and op["kind"] != "AddTable":
            raise _refuse(op["warnings"][0])
        op["verdict"] = verdict
    return {**plan, "target": declared, "dialect": dialect}


def _downgrade_plan(
    live: _LiveDatabase, declared: Dict[str, Any], dialect: str
) -> Dict[str, Any]:
    """The planner run back from the models to the live database. A step
    SQLite can only take by rebuilding the table, or one the renderer has no
    statement for, cannot be undone by this revision: it is irreversible,
    with the reason."""
    declared_json = json.dumps(declared)
    plan = json.loads(
        _core._plan_reverse_from_ir(
            live.schema_ir, declared_json, dialect, _DESTRUCTIVE, live.facts, True
        )
    )
    before = plan["before"]
    plan["operations"] = [
        op
        for op in plan["operations"]
        if "irreversible" in op or op["statements"] or op["warnings"]
    ]
    planner_ops = [
        (index, op)
        for index, op in enumerate(plan["operations"])
        if "irreversible" not in op
        and not op["kind"].startswith(("Restore", "DropForeignKey"))
    ]
    verdicts = json.loads(
        _core._plan_step_verdicts(
            declared_json,
            json.dumps(before),
            dialect,
            "down",
            json.dumps([op for _, op in planner_ops]),
        )
    )
    for (_, op), verdict in zip(planner_ops, verdicts):
        if verdict["needs"] == "rebuild":
            op["irreversible"] = {
                "reason": f"{op['kind']} on {_subject(op)} needs a SQLite table rebuild, "
                f"which an Alembic revision cannot write; `ferro migrate new` writes it"
            }
    for op in plan["operations"]:
        if "irreversible" not in op and not op["statements"] and op.get("warnings"):
            op["irreversible"] = {"reason": op["warnings"][0]}
    return {**plan, "target": before, "dialect": dialect}


try:
    from alembic.autogenerate import comparators as _alembic_comparators
    from alembic.util import DispatchPriority as _AlembicDispatchPriority

    _HAS_ALEMBIC = True
except ImportError:  # pragma: no cover - alembic optional at import time
    _HAS_ALEMBIC = False

if _HAS_ALEMBIC:

    @_alembic_comparators.dispatch_for(
        "schema", priority=_AlembicDispatchPriority.FIRST
    )
    def _compare_ferro_schema(
        autogen_context: Any, upgrade_ops: Any, schemas: Any
    ) -> None:
        """Ferro's one comparator: the planner's revision for ferro's tables.

        It runs first, so the object filter hides ferro's tables before
        Alembic's own comparator looks at any, and its ops go ahead of
        Alembic's (a project table's foreign key to a ferro table finds it;
        the downgrade, reversed, drops ferro's last). Within them the order is
        the planner's, never rearranged here.
        """
        from .translate import FerroRevisionOps, translate

        ferro = _ferro_metadata(autogen_context.metadata)
        if ferro is None:
            return
        object_filter = autogen_context.opts.get("include_object")
        if not isinstance(object_filter, _FerroObjectFilter):
            raise _refuse(
                "target_metadata is ferro's get_metadata(), but this context was "
                "configured without ferro's options, so Alembic would compare ferro's "
                f"tables a second way. Add them in env.py: `{_OPTIONS_LINE}` "
                "(`from ferro.migrations import ferro_options`)"
            )
        dialect = _DIALECTS.get(autogen_context.dialect.name)
        if dialect is None:
            raise _refuse(
                f"the Alembic bridge plans for Postgres and SQLite; this database is "
                f"{autogen_context.dialect.name}"
            )
        tables = ferro.tables
        object_filter.hide(tables)
        live = _read_live(autogen_context.connection, tables)
        if live.tracking_tables:
            raise _refuse(
                "this database is tracked by ferro's in-house migrations (it carries "
                "the _ferro_migrations tracking table), so a change to ferro's models "
                "is a migration `ferro migrate new` writes, not an Alembic revision. "
                "A project whose Alembic chain still manages its own SQLAlchemy tables "
                "drops get_metadata() from env.py's target_metadata and keeps "
                "**ferro_options()"
            )
        up = _upgrade_plan(live, ferro.envelope, dialect)
        if not up["operations"]:
            return
        down = _downgrade_plan(live, ferro.envelope, dialect)
        upgrade = translate(up, direction="up")
        downgrade = translate(down, direction="down")
        _require_enum_rendering(autogen_context, upgrade + downgrade)
        upgrade_ops.ops.insert(0, FerroRevisionOps(upgrade, downgrade))

    def _columns_of(op: Any) -> list[tuple[str, Any]]:
        from alembic.operations import ops as alembic_ops

        from .translate import FerroMarkedOp

        if isinstance(op, FerroMarkedOp):
            return [column for inner in op.wrapped for column in _columns_of(inner)]
        if isinstance(op, alembic_ops.CreateTableOp):
            return [(op.table_name, c) for c in op.columns if isinstance(c, sa.Column)]
        if isinstance(op, alembic_ops.AddColumnOp):
            return [(op.table_name, op.column)]
        return []

    def _require_enum_rendering(autogen_context: Any, operations: list[Any]) -> None:
        """Refuse a revision whose enum column the context cannot write: a
        column of a native enum type renders ``postgresql.ENUM(...,
        create_type=False)`` (the planner creates every type), and only the
        ``render_item`` hook ``ferro_options()`` carries writes the flag —
        ``repr()`` drops it, and the upgrade would then issue ``CREATE TYPE``
        twice. Checked on behaviour, so a project hook that delegates to
        ferro's passes."""
        from sqlalchemy.dialects import postgresql

        render = autogen_context.opts.get("render_item")
        for op in operations:
            for table, column in _columns_of(op):
                if (
                    not isinstance(column.type, postgresql.ENUM)
                    or column.type.create_type
                ):
                    continue
                rendered = (
                    render("type", column.type, autogen_context) if render else False
                )
                if isinstance(rendered, str) and "create_type=False" in rendered:
                    continue
                raise _refuse(
                    f"column {table}.{column.name} is of enum type "
                    f"{column.type.name!r}, which the revision writes as "
                    f"postgresql.ENUM(..., name={column.type.name!r}, create_type=False) "
                    "(the planner creates every enum type itself); this context's "
                    "render_item cannot write create_type=False, and Alembic's default "
                    "rendering drops it. Pass ferro's options in env.py: "
                    f"`{_OPTIONS_LINE}` (a project with its own render_item passes it "
                    "as ferro_options(render_item=...))"
                )
