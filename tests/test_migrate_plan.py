"""Render-level tests for the auto-migrate diff (no live database).

Drives ``_render_migration_sql_for_test`` over both dialects and pins the
exact DDL and warning text the diff produces. Integration behavior (execution,
pool refresh, dependency-aware drops) is covered in ``test_auto_migrate.py``.
"""

import json

import pytest

from ferro._core import _render_migration_sql_for_test
from ferro.ir.compiler import compile_schema_ir_payload, wrap_schema_ir


def _compile_schema_ir_json(schema: dict, name: str) -> str:
    """Compile an ad-hoc schema dict into a SchemaIR envelope JSON string."""
    payload = compile_schema_ir_payload(name, schema)
    return json.dumps(wrap_schema_ir(payload))


def render(schema, live, dialect, *, updates=True, destructive=False, name="Invoice"):
    return _render_migration_sql_for_test(
        name, _compile_schema_ir_json(schema, name.lower()), json.dumps(live), dialect, updates, destructive
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
        assert stmts == ['ALTER TABLE "invoice" ADD COLUMN "paid_date" date_text']
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

    def test_unique_column_strips_inline_unique_on_sqlite(self):
        schema = schema_with({"slug": {"type": "string", "unique": True}})
        stmts, warns = render(schema, PK_ONLY_LIVE, "sqlite")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "slug" varchar',
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_invoice_slug" ON "invoice" ("slug")',
        ]
        assert len(warns) == 1 and "uq_invoice_slug" in warns[0]

        stmts, warns = render(schema, PK_ONLY_LIVE, "postgres")
        assert stmts == ['ALTER TABLE "invoice" ADD COLUMN "slug" varchar UNIQUE']
        assert warns == []

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
            'ALTER TABLE "invoice" ADD FOREIGN KEY ("client_id") REFERENCES "client" ("id")'
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
