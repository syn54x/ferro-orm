"""Render-level tests for the auto-migrate diff (no live database).

Drives ``_render_migration_sql_for_test`` over both dialects and pins the
exact DDL and warning text the diff produces. Integration behavior (execution,
pool refresh, dependency-aware drops) is covered in ``test_auto_migrate.py``.
"""

import json

import pytest

from ferro._core import _render_migration_sql_for_test
from ferro.columns import ColumnSpec, ForeignKeyRef, _enum_values, _logical_type
from ferro.ir.compiler import compile_schema_ir_payload, wrap_schema_ir


def _prop_to_spec(name: str, prop: dict) -> ColumnSpec:
    """Convert one ad-hoc ``schema_with()``-style property dict into a ColumnSpec.

    Mirrors the pre-ColumnSpec ``compile_schema_ir_payload``'s own defaulting
    rules exactly (git history: ``_column_ir``/``_is_nullable`` on the
    dict-based compiler), since these tests exercise the migrate planner from
    a hand-built resolved schema, not from a live Model class:
    - nullable: explicit ``ferro_nullable`` if present, else True (these ad-hoc
      schemas never populate a ``required`` list) — PK always clamps to False.
    - autoincrement: explicit ``autoincrement`` if present, else ``is_pk``.
    - unique/index: explicit or False.
    """
    is_pk = bool(prop.get("primary_key", False))
    nullable_hint = prop.get("ferro_nullable")
    nullable = nullable_hint if isinstance(nullable_hint, bool) else True
    if is_pk:
        nullable = False
    db_type_value = prop.get("db_type")
    db_type_explicit = isinstance(db_type_value, str) and bool(db_type_value)
    enum_type_name = prop.get("enum_type_name")
    enum_values = _enum_values(prop)
    fk_info = prop.get("foreign_key")
    foreign_key = (
        ForeignKeyRef(to_table=fk_info.get("to_table"), on_delete=fk_info.get("on_delete"))
        if isinstance(fk_info, dict)
        else None
    )
    return ColumnSpec(
        name=name,
        logical_type=_logical_type(prop),
        nullable=nullable,
        primary_key=is_pk,
        autoincrement=bool(prop.get("autoincrement", is_pk)),
        unique=bool(prop.get("unique", False)),
        index=bool(prop.get("index", False)),
        default=prop.get("default"),
        format=prop.get("format"),
        python_type=None,
        enum_values=tuple(enum_values) if isinstance(enum_values, list) else None,
        enum_type_name=enum_type_name if isinstance(enum_type_name, str) and enum_type_name else None,
        db_type=db_type_value if db_type_explicit else None,
        db_type_explicit=db_type_explicit,
        foreign_key=foreign_key,
    )


def _compile_schema_ir_json(schema: dict, name: str) -> str:
    """Compile an ad-hoc schema dict into a SchemaIR envelope JSON string."""
    properties = schema.get("properties", {})
    specs = [_prop_to_spec(col_name, prop) for col_name, prop in properties.items()]
    composite_indexes = schema.get("ferro_composite_indexes") or []
    composite_uniques = schema.get("ferro_composite_uniques") or []
    payload = compile_schema_ir_payload(
        name,
        specs,
        table_name=name,
        composite_indexes=composite_indexes,
        composite_uniques=composite_uniques,
    )
    return json.dumps(wrap_schema_ir(payload))


def render(schema, live, dialect, *, updates=True, destructive=False, name="Invoice"):
    return _render_migration_sql_for_test(
        name.lower(), _compile_schema_ir_json(schema, name.lower()), json.dumps(live), dialect, updates, destructive
    )


PK_ONLY_LIVE = [
    {
        "name": "id",
        "declared_type": "integer",
        "is_primary_key": True,
        "is_nullable": False,
    }
]


def schema_with(props):
    return {"properties": {"id": {"type": "integer", "primary_key": True}, **props}}


class TestAddColumn:
    def test_nullable_column_add_uses_create_table_type_spelling(self):
        schema = schema_with(
            {"paid_date": {"type": "string", "db_type": "date", "ferro_nullable": True}}
        )
        stmts, warns = render(schema, PK_ONLY_LIVE, "sqlite")
        assert stmts == ['ALTER TABLE "invoice" ADD COLUMN "paid_date" DATE']
        assert warns == []

        stmts, warns = render(schema, PK_ONLY_LIVE, "postgres")
        assert stmts == ['ALTER TABLE "invoice" ADD COLUMN "paid_date" date']
        assert warns == []

    def test_not_null_with_literal_default_backfills(self):
        schema = schema_with(
            {"status": {"type": "string", "ferro_nullable": False, "default": "draft"}}
        )
        stmts, _ = render(schema, PK_ONLY_LIVE, "postgres")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "status" varchar NOT NULL DEFAULT \'draft\'',
            'ALTER TABLE "invoice" ALTER COLUMN "status" DROP DEFAULT',
        ]

        # SQLite cannot DROP DEFAULT; the backfill default remains (documented).
        stmts, _ = render(schema, PK_ONLY_LIVE, "sqlite")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "status" varchar NOT NULL DEFAULT \'draft\'',
        ]

    def test_not_null_without_default_fails_loudly(self):
        schema = schema_with(
            {
                "created_at": {
                    "type": "string",
                    "format": "date-time",
                    "ferro_nullable": False,
                }
            }
        )
        for dialect in ("sqlite", "postgres"):
            with pytest.raises(ValueError, match=r"invoice\.created_at.*Alembic"):
                render(schema, PK_ONLY_LIVE, dialect)

    def test_adding_primary_key_column_fails_loudly(self):
        schema = schema_with({})
        live = [{"name": "name", "declared_type": "varchar"}]
        with pytest.raises(ValueError, match=r"invoice\.id.*primary key"):
            render(schema, live, "sqlite")

    def test_unique_column_add_is_standalone_named_index_on_both_dialects(self):
        # FF-B B4/D1: the standalone named uq_ index is the canonical unique
        # shape on both dialects; no inline UNIQUE, no compromise warning.
        schema = schema_with({"slug": {"type": "string", "unique": True}})
        for dialect in ("sqlite", "postgres"):
            stmts, warns = render(schema, PK_ONLY_LIVE, dialect)
            assert stmts == [
                'ALTER TABLE "invoice" ADD COLUMN "slug" varchar',
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_invoice_slug" ON "invoice" ("slug")',
            ], dialect
            assert warns == [], dialect

    def test_indexed_column_add_emits_create_index(self):
        schema = schema_with({"kind": {"type": "string", "index": True}})
        for dialect in ("sqlite", "postgres"):
            stmts, _ = render(schema, PK_ONLY_LIVE, dialect)
            assert stmts == [
                'ALTER TABLE "invoice" ADD COLUMN "kind" varchar',
                'CREATE INDEX IF NOT EXISTS "idx_invoice_kind" ON "invoice" ("kind")',
            ]

    def test_fk_shadow_column_is_capability_relative(self):
        schema = schema_with(
            {
                "client_id": {
                    "type": "integer",
                    "foreign_key": {"to_table": "client", "on_delete": "CASCADE"},
                }
            }
        )
        stmts, warns = render(schema, PK_ONLY_LIVE, "postgres")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "client_id" integer',
            'ALTER TABLE "invoice" ADD CONSTRAINT "fk_invoice_client_id_client"'
            ' FOREIGN KEY ("client_id") REFERENCES "client" ("id")'
            " ON DELETE CASCADE",
        ]
        assert warns == []

        stmts, warns = render(schema, PK_ONLY_LIVE, "sqlite")
        assert stmts == ['ALTER TABLE "invoice" ADD COLUMN "client_id" integer']
        assert len(warns) == 1 and "FOREIGN KEY" in warns[0] and "Alembic" in warns[0]


class TestReconcileExisting:
    def test_pg_type_mismatch_emits_alter_with_using_cast(self):
        schema = schema_with(
            {"total": {"type": "integer", "db_type": "bigint", "ferro_nullable": False}}
        )
        live = PK_ONLY_LIVE + [
            {"name": "total", "declared_type": "integer", "is_nullable": False}
        ]
        stmts, _ = render(schema, live, "postgres")
        assert stmts == [
            'ALTER TABLE "invoice" ALTER COLUMN "total" TYPE bigint USING "total"::bigint'
        ]

    def test_pg_nullability_mismatch_emits_set_and_drop_not_null(self):
        schema = schema_with(
            {
                "a": {"type": "string", "ferro_nullable": False},
                "b": {"type": "string", "ferro_nullable": True},
            }
        )
        live = PK_ONLY_LIVE + [
            {"name": "a", "declared_type": "character varying", "is_nullable": True},
            {"name": "b", "declared_type": "character varying", "is_nullable": False},
        ]
        stmts, _ = render(schema, live, "postgres")
        assert 'ALTER TABLE "invoice" ALTER COLUMN "a" SET NOT NULL' in stmts
        assert 'ALTER TABLE "invoice" ALTER COLUMN "b" DROP NOT NULL' in stmts

    def test_pg_native_enum_columns_are_left_to_alembic(self):
        schema = schema_with({"status": {"type": "string"}})
        live = PK_ONLY_LIVE + [
            {"name": "status", "declared_type": "USER-DEFINED", "is_enum_udt": True}
        ]
        stmts, warns = render(schema, live, "postgres")
        assert stmts == []
        assert warns == []

    def test_sqlite_type_drift_warns_and_emits_no_ddl(self):
        schema = schema_with({"count": {"type": "integer"}})
        live = PK_ONLY_LIVE + [{"name": "count", "declared_type": "varchar"}]
        stmts, warns = render(schema, live, "sqlite")
        assert stmts == []
        assert len(warns) == 1
        assert "invoice.count" in warns[0] and "Alembic" in warns[0]

    def test_sqlite_cosmetic_spelling_differences_do_not_warn(self):
        # An Alembic-created table spells temporal/uuid types differently than
        # the runtime emitter; SQLite type affinity makes that equivalent.
        schema = schema_with(
            {
                "created_at": {"type": "string", "format": "date-time"},
                "ref": {"type": "string", "format": "uuid"},
            }
        )
        live = PK_ONLY_LIVE + [
            {"name": "created_at", "declared_type": "DATETIME"},
            {"name": "ref", "declared_type": "CHAR(32)"},
        ]
        stmts, warns = render(schema, live, "sqlite")
        assert stmts == []
        assert warns == []


class TestDestructive:
    LIVE_WITH_EXTRA = PK_ONLY_LIVE + [{"name": "legacy_notes", "declared_type": "text"}]

    def test_removed_column_drops_only_with_flag(self):
        schema = schema_with({})
        stmts, _ = render(schema, self.LIVE_WITH_EXTRA, "sqlite", destructive=True)
        assert stmts == ['ALTER TABLE "invoice" DROP COLUMN "legacy_notes"']

        stmts, _ = render(schema, self.LIVE_WITH_EXTRA, "sqlite", destructive=False)
        assert stmts == []

    def test_live_primary_key_missing_from_model_fails_loudly(self):
        schema = {"properties": {"name": {"type": "string"}}}
        live = PK_ONLY_LIVE + [{"name": "name", "declared_type": "varchar"}]
        with pytest.raises(ValueError, match=r"invoice\.id.*primary key.*Alembic"):
            render(schema, live, "sqlite", destructive=True)

    def test_destructive_implies_updates(self):
        schema = schema_with({"memo": {"type": "string", "ferro_nullable": True}})
        stmts, _ = render(
            schema, PK_ONLY_LIVE, "sqlite", updates=False, destructive=True
        )
        assert stmts == ['ALTER TABLE "invoice" ADD COLUMN "memo" varchar']


def test_updates_false_produces_no_plan():
    schema = schema_with({"memo": {"type": "string", "ferro_nullable": True}})
    stmts, warns = render(schema, PK_ONLY_LIVE, "sqlite", updates=False)
    assert stmts == []
    assert warns == []


def test_unknown_dialect_is_rejected():
    with pytest.raises(ValueError, match="Unknown dialect"):
        render(schema_with({}), PK_ONLY_LIVE, "mysql")


# ---------------------------------------------------------------------------
# Index no-op guard (issue #144) — unit-level assertion
# ---------------------------------------------------------------------------

# Live-column list for IdxNoopModel: id (PK) + x + y
_NOOP_LIVE_COLUMNS = [
    {
        "name": "id",
        "declared_type": "integer",
        "is_primary_key": True,
        "is_nullable": False,
    },
    {"name": "x", "declared_type": "integer", "is_nullable": False},
    {"name": "y", "declared_type": "integer", "is_nullable": False},
]

# Schema with a composite index over (x, y).
_NOOP_SCHEMA = {
    "properties": {
        "id": {"type": "integer", "primary_key": True},
        "x": {"type": "integer", "ferro_nullable": False},
        "y": {"type": "integer", "ferro_nullable": False},
    },
    "ferro_composite_indexes": [["x", "y"]],
}

# Canonical index name the planner would have created.
_NOOP_LIVE_INDEXES = [
    {"name": "idx_idxnoopmodel_x_y", "columns": ["x", "y"], "unique": False}
]


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_index_noop_emits_zero_ddl_when_index_already_present(dialect):
    """Planner must produce an empty statement list when the composite index
    already exists in the live schema — no DROP INDEX + CREATE INDEX churn
    (false-alarm class of bug, issue #144)."""
    stmts, warns = _render_migration_sql_for_test(
        "idxnoopmodel",
        _compile_schema_ir_json(_NOOP_SCHEMA, "idxnoopmodel"),
        json.dumps(_NOOP_LIVE_COLUMNS),
        dialect,
        True,   # updates
        False,  # destructive
        json.dumps(_NOOP_LIVE_INDEXES),
    )
    assert stmts == [], (
        f"[{dialect}] expected no DDL when index already present, got: {stmts}"
    )
    assert warns == [], (
        f"[{dialect}] expected no warnings when index already present, got: {warns}"
    )


@pytest.mark.parametrize("dialect", ["sqlite"])
def test_derived_uuid_column_does_not_drift_when_consuming_python_ir(dialect):
    """A derived uuid column (no explicit db_type) must produce no DDL and no warning
    when the live column has type 'uuid_text' — the storage-token comparison path
    (Task 1 fix) must handle the None db_type case correctly."""
    schema_ir = json.dumps({
        "ir_kind": "schema", "ir_version": 1,
        "payload": {"dialect_agnostic": True, "models": [{
            "model_name": "acct",
            "table_name": "acct",
            "columns": [{"name": "id", "logical_type": "uuid", "format": "uuid",
                         "nullable": False, "primary_key": True,
                         "autoincrement": False, "unique": False, "index": False,
                         "default": None}],
            "indexes": [], "uniques": [], "foreign_keys": [], "checks": [],
        }]},
    })
    live = json.dumps([{"name": "id", "declared_type": "uuid_text",
                        "is_nullable": False, "is_primary_key": True}])
    stmts, warns = _render_migration_sql_for_test("acct", schema_ir, live, dialect)
    assert stmts == [], f"unexpected DDL: {stmts}"
    assert warns == [], f"unexpected drift warning: {warns}"


class TestJsonStorageTokens:
    """ADR-0004: jsonb is a Postgres-only canonical; SQLite lowers to JSON."""

    def test_jsonb_add_column_renders_jsonb_on_postgres(self):
        schema = schema_with(
            {"payload": {"type": "object", "db_type": "jsonb", "ferro_nullable": True}}
        )
        stmts, warns = render(schema, PK_ONLY_LIVE, "postgres")
        assert stmts == ['ALTER TABLE "invoice" ADD COLUMN "payload" jsonb']
        assert warns == []

    def test_jsonb_add_column_lowers_to_json_on_sqlite(self):
        schema = schema_with(
            {"payload": {"type": "object", "db_type": "jsonb", "ferro_nullable": True}}
        )
        stmts, warns = render(schema, PK_ONLY_LIVE, "sqlite")
        assert stmts == ['ALTER TABLE "invoice" ADD COLUMN "payload" JSON']
        assert warns == []

    def test_explicit_json_add_column_renders_json_on_postgres(self):
        schema = schema_with(
            {"payload": {"type": "object", "db_type": "json", "ferro_nullable": True}}
        )
        stmts, warns = render(schema, PK_ONLY_LIVE, "postgres")
        assert stmts == ['ALTER TABLE "invoice" ADD COLUMN "payload" json']
        assert warns == []

    def test_jsonb_array_add_column_renders_jsonb_on_postgres(self):
        schema = schema_with(
            {"entries": {"type": "array", "db_type": "jsonb", "ferro_nullable": True}}
        )
        stmts, _ = render(schema, PK_ONLY_LIVE, "postgres")
        assert stmts == ['ALTER TABLE "invoice" ADD COLUMN "entries" jsonb']
