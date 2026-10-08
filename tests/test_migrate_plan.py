"""Render-level tests for the auto-migrate diff (no live database).

Drives ``_render_migration_sql_for_test`` over both dialects and pins the
exact DDL and warning text the diff produces. Integration behavior (execution,
pool refresh, dependency-aware drops) is covered in ``test_auto_migrate.py``.
"""

import json

import pytest

from typing import Annotated

from ferro._core import (
    _live_schema_ir,
    _plan_from_ir,
    _render_migration_sql_for_test,
)
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
        db_check=prop.get("db_check") is True,
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

    def test_not_null_json_object_default_backfills_with_storage_cast(self):
        """#373: a JSON object default is a backfill literal on json-family storage."""
        derived = schema_with(
            {
                "turns": {
                    "type": "object",
                    "ferro_nullable": False,
                    "default": {},
                }
            }
        )
        stmts, _ = render(derived, PK_ONLY_LIVE, "postgres")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "turns" jsonb NOT NULL DEFAULT \'{}\'::jsonb',
            'ALTER TABLE "invoice" ALTER COLUMN "turns" DROP DEFAULT',
        ]
        stmts, _ = render(derived, PK_ONLY_LIVE, "sqlite")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "turns" JSON NOT NULL DEFAULT \'{}\'',
        ]

        explicit_json = schema_with(
            {
                "turns": {
                    "type": "object",
                    "db_type": "json",
                    "ferro_nullable": False,
                    "default": {},
                }
            }
        )
        stmts, _ = render(explicit_json, PK_ONLY_LIVE, "postgres")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "turns" json NOT NULL DEFAULT \'{}\'::json',
            'ALTER TABLE "invoice" ALTER COLUMN "turns" DROP DEFAULT',
        ]

    def test_not_null_json_array_default_backfills(self):
        schema = schema_with(
            {
                "tags": {
                    "type": "array",
                    "ferro_nullable": False,
                    "default": [],
                }
            }
        )
        stmts, _ = render(schema, PK_ONLY_LIVE, "postgres")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "tags" jsonb NOT NULL DEFAULT \'[]\'::jsonb',
            'ALTER TABLE "invoice" ALTER COLUMN "tags" DROP DEFAULT',
        ]

    def test_json_object_default_on_non_json_storage_still_refused(self):
        schema = schema_with(
            {
                "note": {
                    "type": "string",
                    "ferro_nullable": False,
                    "default": {},
                }
            }
        )
        for dialect in ("sqlite", "postgres"):
            with pytest.raises(ValueError, match=r"invoice\.note.*ferro migrate new"):
                render(schema, PK_ONLY_LIVE, dialect)

    def test_not_null_json_without_default_fails_loudly(self):
        """Unset json-family default (failed factory snapshot) uses today's refusal."""
        schema = schema_with(
            {"turns": {"type": "object", "ferro_nullable": False}}
        )
        for dialect in ("sqlite", "postgres"):
            with pytest.raises(ValueError, match=r"invoice\.turns.*ferro migrate new"):
                render(schema, PK_ONLY_LIVE, dialect)

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
            with pytest.raises(
                ValueError, match=r"invoice\.created_at.*ferro migrate new"
            ):
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

        # #514: a nullable added FK column (default NULL) is the one shape
        # SQLite's ADD COLUMN accepts a REFERENCES clause for.
        stmts, warns = render(schema, PK_ONLY_LIVE, "sqlite")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "client_id" integer'
            ' REFERENCES "client"("id") ON DELETE CASCADE'
        ]
        assert warns == []

    def test_not_null_fk_column_with_default_warns_naming_migrations_on_sqlite(self):
        schema = schema_with(
            {
                "client_id": {
                    "type": "integer",
                    "ferro_nullable": False,
                    "default": 1,
                    "foreign_key": {"to_table": "client", "on_delete": "RESTRICT"},
                }
            }
        )
        stmts, warns = render(schema, PK_ONLY_LIVE, "sqlite")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "client_id" integer NOT NULL DEFAULT 1'
        ]
        assert len(warns) == 1
        assert "invoice.client_id" in warns[0] and "FOREIGN KEY" in warns[0]
        assert "ferro migrate new" in warns[0] and "Alembic" not in warns[0]

        # Postgres is unchanged: add, drop the backfill default, named constraint.
        stmts, warns = render(schema, PK_ONLY_LIVE, "postgres")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "client_id" integer NOT NULL DEFAULT 1',
            'ALTER TABLE "invoice" ALTER COLUMN "client_id" DROP DEFAULT',
            'ALTER TABLE "invoice" ADD CONSTRAINT "fk_invoice_client_id_client"'
            ' FOREIGN KEY ("client_id") REFERENCES "client" ("id")'
            " ON DELETE RESTRICT",
        ]
        assert warns == []

    def test_db_check_column_add_is_inline_on_sqlite_and_an_alter_on_postgres(self):
        schema = schema_with(
            {
                "status": {
                    "type": "string",
                    "enum": ["draft", "paid"],
                    "db_type": "text",
                    "db_check": True,
                    "ferro_nullable": True,
                }
            }
        )
        stmts, warns = render(schema, PK_ONLY_LIVE, "sqlite")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "status" text'
            " CONSTRAINT \"ck_invoice_status\" CHECK (\"status\" IN ('draft', 'paid'))"
        ]
        assert warns == []

        # Postgres keeps its post-add idempotent ALTER, byte-identical.
        stmts, warns = render(schema, PK_ONLY_LIVE, "postgres")
        assert stmts == [
            'ALTER TABLE "invoice" ADD COLUMN "status" text',
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_invoice_status' AND conrelid = '\"invoice\"'::regclass) THEN "
            'ALTER TABLE "invoice" ADD CONSTRAINT "ck_invoice_status" '
            "CHECK (\"status\" IN ('draft', 'paid')); END IF; END $$",
        ]
        assert warns == []


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

    def test_pg_native_enum_column_moved_to_a_scalar_reports_the_recipe(self):
        """A live native-enum column the model now declares ``str``: no
        statement converts it in place, so the pass runs none and says so
        in the generator's words (``EnumTypeMove``, ADR-0052)."""
        schema = schema_with({"status": {"type": "string"}})
        live = PK_ONLY_LIVE + [
            {"name": "status", "declared_type": "USER-DEFINED", "is_enum_udt": True}
        ]
        stmts, warns = render(schema, live, "postgres")
        assert stmts == []
        assert warns == [
            'changing "invoice"."status" to or from a native enum type is not '
            "generated: add a column of the new type, copy the values across in a "
            "data step (ferro migrate new --data-step …), then drop the old column"
        ]

    def test_sqlite_type_drift_warns_and_emits_no_ddl(self):
        schema = schema_with({"count": {"type": "integer"}})
        live = PK_ONLY_LIVE + [{"name": "count", "declared_type": "varchar"}]
        stmts, warns = render(schema, live, "sqlite")
        assert stmts == []
        assert len(warns) == 1
        assert "invoice.count" in warns[0] and "ferro migrate new" in warns[0]
        assert "Alembic" not in warns[0]

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
        with pytest.raises(
            ValueError, match=r"invoice\.id.*primary key.*ferro migrate new"
        ):
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


class TestJsonStorageDiff:
    """#263: introspection distinguishes jsonb; json<->jsonb is one ALTER on
    Postgres and a no-op on SQLite (ADR-0004)."""

    def test_live_jsonb_column_produces_no_phantom_diff(self):
        schema = schema_with(
            {"payload": {"type": "object", "db_type": "jsonb", "ferro_nullable": True}}
        )
        live = PK_ONLY_LIVE + [
            {"name": "payload", "declared_type": "jsonb", "is_nullable": True}
        ]
        stmts, warns = render(schema, live, "postgres")
        assert stmts == []
        assert warns == []

    def test_live_json_column_with_default_declaration_upgrades_to_jsonb(self):
        """Default flip (ADR-0005): a derived json-family field now means jsonb
        on Postgres, so a pre-flip live json column upgrades with one ALTER."""
        schema = schema_with(
            {"payload": {"type": "object", "ferro_nullable": True}}
        )
        live = PK_ONLY_LIVE + [
            {"name": "payload", "declared_type": "json", "is_nullable": True}
        ]
        stmts, warns = render(schema, live, "postgres")
        assert stmts == [
            'ALTER TABLE "invoice" ALTER COLUMN "payload" TYPE jsonb USING "payload"::jsonb'
        ]
        assert warns == []

    def test_live_json_column_with_explicit_json_opt_out_no_diff(self):
        """db_type="json" is the opt-out — existing plain-json columns keep
        their storage with zero operations."""
        schema = schema_with(
            {"payload": {"type": "object", "db_type": "json", "ferro_nullable": True}}
        )
        live = PK_ONLY_LIVE + [
            {"name": "payload", "declared_type": "json", "is_nullable": True}
        ]
        stmts, warns = render(schema, live, "postgres")
        assert stmts == []
        assert warns == []

    def test_live_jsonb_column_with_default_declaration_no_diff(self):
        """The post-flip steady state: derived declaration + live jsonb agree."""
        schema = schema_with(
            {"payload": {"type": "object", "ferro_nullable": True}}
        )
        live = PK_ONLY_LIVE + [
            {"name": "payload", "declared_type": "jsonb", "is_nullable": True}
        ]
        stmts, warns = render(schema, live, "postgres")
        assert stmts == []
        assert warns == []

    def test_json_to_jsonb_declaration_edit_is_exactly_one_alter(self):
        schema = schema_with(
            {"payload": {"type": "object", "db_type": "jsonb", "ferro_nullable": True}}
        )
        live = PK_ONLY_LIVE + [
            {"name": "payload", "declared_type": "json", "is_nullable": True}
        ]
        stmts, warns = render(schema, live, "postgres")
        assert stmts == [
            'ALTER TABLE "invoice" ALTER COLUMN "payload" TYPE jsonb USING "payload"::jsonb'
        ]
        assert warns == []

    def test_jsonb_to_json_declaration_edit_is_the_mirror_alter(self):
        """Declaring the explicit json opt-out over a live jsonb column is the
        reverse edit (post-ADR-0005, the bare declaration means jsonb)."""
        schema = schema_with(
            {"payload": {"type": "object", "db_type": "json", "ferro_nullable": True}}
        )
        live = PK_ONLY_LIVE + [
            {"name": "payload", "declared_type": "jsonb", "is_nullable": True}
        ]
        stmts, warns = render(schema, live, "postgres")
        assert stmts == [
            'ALTER TABLE "invoice" ALTER COLUMN "payload" TYPE json USING "payload"::json'
        ]
        assert warns == []

    def test_json_jsonb_edit_is_noop_on_sqlite(self):
        """Both tokens lower to the same SQLite storage — no drift either way."""
        schema = schema_with(
            {"payload": {"type": "object", "db_type": "jsonb", "ferro_nullable": True}}
        )
        live = PK_ONLY_LIVE + [
            {"name": "payload", "declared_type": "JSON", "is_nullable": True}
        ]
        stmts, warns = render(schema, live, "sqlite")
        assert stmts == []
        assert warns == []


class TestForeignKeyReconcile:
    """Plan-level rendering for FK definition drift on existing columns (#325)."""

    LIVE_WITH_FK_COLUMN = PK_ONLY_LIVE + [
        {"name": "connection_id", "declared_type": "integer", "is_nullable": True}
    ]
    LIVE_FK_CASCADE = [
        {
            "name": "fk_invoice_connection_id_connection",
            "column": "connection_id",
            "to_table": "connection",
            "to_column": "id",
            "on_delete": "CASCADE",
        }
    ]

    def _schema(self, on_delete):
        return schema_with(
            {
                "connection_id": {
                    "type": "integer",
                    "ferro_nullable": True,
                    "foreign_key": {"to_table": "connection", "on_delete": on_delete},
                }
            }
        )

    def _render(self, dialect, *, live_fks, on_delete="SET NULL"):
        schema = self._schema(on_delete)
        return _render_migration_sql_for_test(
            "invoice",
            _compile_schema_ir_json(schema, "invoice"),
            json.dumps(self.LIVE_WITH_FK_COLUMN),
            dialect,
            True,
            False,
            "",
            json.dumps(live_fks),
        )

    def test_pg_on_delete_drift_rebuilds_constraint(self):
        stmts, warns = self._render("postgres", live_fks=self.LIVE_FK_CASCADE)
        assert stmts == [
            'ALTER TABLE "invoice" DROP CONSTRAINT "fk_invoice_connection_id_connection"',
            'ALTER TABLE "invoice" ADD CONSTRAINT "fk_invoice_connection_id_connection"'
            ' FOREIGN KEY ("connection_id") REFERENCES "connection" ("id")'
            " ON DELETE SET NULL",
        ]
        assert warns == []

    def test_pg_matching_on_delete_is_noop(self):
        stmts, warns = self._render(
            "postgres", live_fks=self.LIVE_FK_CASCADE, on_delete="CASCADE"
        )
        assert stmts == []
        assert warns == []

    def test_pg_missing_fk_on_existing_column_is_added(self):
        stmts, warns = self._render("postgres", live_fks=[])
        assert stmts == [
            'ALTER TABLE "invoice" ADD CONSTRAINT "fk_invoice_connection_id_connection"'
            ' FOREIGN KEY ("connection_id") REFERENCES "connection" ("id")'
            " ON DELETE SET NULL",
        ]
        assert warns == []

    def test_pg_user_owned_fk_drift_warns_and_leaves_constraint(self):
        user_fk = [dict(self.LIVE_FK_CASCADE[0], name="invoice_connection_id_fkey")]
        stmts, warns = self._render("postgres", live_fks=user_fk)
        assert stmts == []
        assert len(warns) == 1
        assert "not ferro-owned" in warns[0]
        assert "invoice_connection_id_fkey" in warns[0]

    def test_sqlite_on_delete_drift_warns_and_emits_no_ddl(self):
        unnamed_fk = [dict(self.LIVE_FK_CASCADE[0], name=None)]
        stmts, warns = self._render("sqlite", live_fks=unnamed_fk)
        assert stmts == []
        assert len(warns) == 1
        assert "on_delete SET NULL" in warns[0]
        assert "CASCADE" in warns[0]
        assert "ferro migrate new" in warns[0]
        assert "Alembic" not in warns[0]


class TestValidityFlags:
    """#515 (ADR-0043, ADR-0044): a live constraint or index can exist and
    still not be trusted. A declared FK or check that exists ``NOT VALID`` is
    validated in place; a declared index that exists invalid is rebuilt. The
    flags default to ``true`` so live fixtures without them (and every SQLite
    table) plan exactly what they planned before."""

    LIVE_COLUMNS = PK_ONLY_LIVE + [
        {"name": "client_id", "declared_type": "integer", "is_nullable": True},
        {"name": "owner_id", "declared_type": "integer", "is_nullable": True},
        {"name": "status", "declared_type": "text", "is_nullable": True},
        {"name": "kind", "declared_type": "text", "is_nullable": True},
        {"name": "slug", "declared_type": "character varying", "is_nullable": True},
        {"name": "x", "declared_type": "integer", "is_nullable": True},
        {"name": "y", "declared_type": "integer", "is_nullable": True},
    ]

    STATUS_CHECK_DEF = "CHECK ((status = ANY (ARRAY['draft'::text, 'paid'::text])))"

    VALIDATE_STATUS = 'ALTER TABLE "invoice" VALIDATE CONSTRAINT "ck_invoice_status"'
    VALIDATE_CLIENT_FK = (
        'ALTER TABLE "invoice" VALIDATE CONSTRAINT "fk_invoice_client_id_client"'
    )

    def _schema(self, *, everything=False):
        props = {
            "client_id": {
                "type": "integer",
                "foreign_key": {"to_table": "client", "on_delete": "CASCADE"},
            },
            "status": {
                "type": "string",
                "enum": ["draft", "paid"],
                "db_type": "text",
                "db_check": True,
            },
            "slug": {"type": "string", "unique": True},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
        }
        if everything:
            props["owner_id"] = {
                "type": "integer",
                "foreign_key": {"to_table": "owner", "on_delete": "CASCADE"},
            }
            props["kind"] = {
                "type": "string",
                "enum": ["a", "b"],
                "db_type": "text",
                "db_check": True,
            }
        schema = schema_with(props)
        if everything:
            schema["ferro_composite_indexes"] = [["x", "y"]]
        return schema

    def _live_fks(self, *, validated):
        fk = {
            "name": "fk_invoice_client_id_client",
            "column": "client_id",
            "to_table": "client",
            "to_column": "id",
            "on_delete": "CASCADE",
        }
        if validated is not None:
            fk["validated"] = validated
        return [fk]

    def _live_checks(self, *, validated, definition=None):
        check = {
            "name": "ck_invoice_status",
            "definition": definition or self.STATUS_CHECK_DEF,
            "ferro_owned": True,
        }
        if validated is not None:
            check["validated"] = validated
        return [check]

    def _live_indexes(self, *, valid):
        index = {"name": "uq_invoice_slug", "columns": ["slug"], "unique": True}
        if valid is not None:
            index["valid"] = valid
        return [index]

    def _render(self, dialect, *, schema=None, fks=None, checks=None, indexes=None):
        return _render_migration_sql_for_test(
            "invoice",
            _compile_schema_ir_json(schema or self._schema(), "invoice"),
            json.dumps(self.LIVE_COLUMNS),
            dialect,
            True,
            False,
            json.dumps(
                indexes if indexes is not None else self._live_indexes(valid=True)
            ),
            json.dumps(fks if fks is not None else self._live_fks(validated=True)),
            json.dumps(
                checks if checks is not None else self._live_checks(validated=True)
            ),
        )

    @pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
    def test_fixtures_without_the_flags_still_load_and_plan_nothing(self, dialect):
        stmts, warns = self._render(
            dialect,
            fks=self._live_fks(validated=None),
            checks=self._live_checks(validated=None),
            indexes=self._live_indexes(valid=None),
        )
        assert stmts == []
        assert warns == []

    def test_pg_not_valid_check_is_one_validate_statement(self):
        stmts, warns = self._render(
            "postgres",
            checks=self._live_checks(
                validated=False, definition=self.STATUS_CHECK_DEF + " NOT VALID"
            ),
        )
        assert stmts == [self.VALIDATE_STATUS]
        assert warns == []

    def test_pg_not_valid_fk_is_one_validate_statement(self):
        stmts, warns = self._render("postgres", fks=self._live_fks(validated=False))
        assert stmts == [self.VALIDATE_CLIENT_FK]
        assert warns == []

    def test_pg_invalid_index_is_dropped_then_created_with_the_add_statement(self):
        stmts, warns = self._render("postgres", indexes=self._live_indexes(valid=False))
        assert stmts == [
            'DROP INDEX "uq_invoice_slug"',
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_invoice_slug" ON "invoice" ("slug")',
        ]
        assert warns == []

    def test_pg_not_valid_check_whose_body_drifted_is_one_rebuild_and_no_validate(self):
        drifted = "CHECK ((status = ANY (ARRAY['draft'::text]))) NOT VALID"
        stmts, warns = self._render(
            "postgres", checks=self._live_checks(validated=False, definition=drifted)
        )
        assert stmts == [
            'ALTER TABLE "invoice" DROP CONSTRAINT "ck_invoice_status"',
            'ALTER TABLE "invoice" ADD CONSTRAINT "ck_invoice_status" '
            "CHECK (\"status\" IN ('draft', 'paid'))",
        ]
        assert warns == []

    def test_pg_order_rebuild_index_with_adds_and_validates_after_constraint_adds(self):
        """Acceptance criterion 4: ``RebuildIndex`` lands where ``AddIndex``
        goes (after the column ops, before the foreign-key ops), and every
        ``ValidateConstraint`` after every ``AddCheck`` / ``AddForeignKey``."""
        stmts, warns = self._render(
            "postgres",
            schema=self._schema(everything=True),
            fks=self._live_fks(validated=False),
            checks=self._live_checks(
                validated=False, definition=self.STATUS_CHECK_DEF + " NOT VALID"
            ),
            indexes=self._live_indexes(valid=False),
        )
        assert stmts == [
            'CREATE INDEX IF NOT EXISTS "idx_invoice_x_y" ON "invoice" ("x", "y")',
            'DROP INDEX "uq_invoice_slug"',
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_invoice_slug" ON "invoice" ("slug")',
            'ALTER TABLE "invoice" ADD CONSTRAINT "fk_invoice_owner_id_owner"'
            ' FOREIGN KEY ("owner_id") REFERENCES "owner" ("id") ON DELETE CASCADE',
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_invoice_kind' AND conrelid = '\"invoice\"'::regclass) THEN "
            'ALTER TABLE "invoice" ADD CONSTRAINT "ck_invoice_kind" '
            "CHECK (\"kind\" IN ('a', 'b')); END IF; END $$",
            self.VALIDATE_CLIENT_FK,
            self.VALIDATE_STATUS,
        ]
        assert warns == []

    def test_sqlite_plans_are_byte_unchanged_by_the_flags(self):
        """SQLite introspection always reports ``true``, which is the serde
        default: a SQLite plan with the flags omitted is byte-identical to
        one with them, and neither carries a validate or a rebuild."""
        baseline = self._render("sqlite", schema=self._schema(everything=True))
        assert baseline == self._render(
            "sqlite",
            schema=self._schema(everything=True),
            fks=self._live_fks(validated=None),
            checks=self._live_checks(validated=None),
            indexes=self._live_indexes(valid=None),
        )
        stmts, _ = baseline
        assert not any(
            "VALIDATE" in sql or sql.startswith("DROP INDEX") for sql in stmts
        )


# ---------------------------------------------------------------------------
# The one planner over FFI (#517): ``_plan_from_ir`` decides every change
# between two SchemaIR envelopes for the whole modelset; ``_live_schema_ir``
# reads a live database into the same input.
# ---------------------------------------------------------------------------

EMPTY_MODELSET = json.dumps(
    {
        "ir_kind": "schema",
        "ir_version": 1,
        "payload": {"dialect_agnostic": True, "models": []},
    }
)


def _declare_library_models():
    """``planauthor`` and ``planbook`` (FK → planauthor, a native-enum status
    on Postgres, a db_check'd text kind, an index and a composite unique),
    declared child first so the planner, not declaration order, decides."""
    from enum import StrEnum

    from ferro import BackRef, ForeignKey, Model, Relation
    from ferro.base import FerroField

    class PlanStatus(StrEnum):
        DRAFT = "draft"
        PUBLISHED = "published"

    class PlanKind(StrEnum):
        NOVEL = "novel"
        ESSAY = "essay"

    class PlanBook(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        author: Annotated["PlanAuthor", ForeignKey(related_name="books")]
        title: Annotated[str, FerroField(index=True)]
        status: PlanStatus = PlanStatus.DRAFT
        kind: Annotated[PlanKind, FerroField(db_type="text", db_check=True)] = (
            PlanKind.NOVEL
        )

    class PlanAuthor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(unique=True)]
        books: Relation[list[PlanBook]] = BackRef()

    return PlanAuthor, PlanBook


def _declared_modelset() -> str:
    from ferro.ir.compiler import compile_registry_schema_ir

    return json.dumps(compile_registry_schema_ir())


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_plan_from_an_empty_modelset_adds_every_model_parents_first(
    dialect, clean_registry
):
    _declare_library_models()
    declared = _declared_modelset()

    plan = json.loads(
        _plan_from_ir(EMPTY_MODELSET, declared, dialect, '{"destructive": false}')
    )
    kinds = [
        (op["kind"], op.get("table", op.get("type_name"))) for op in plan["operations"]
    ]
    tables = [("AddTable", "planauthor"), ("AddTable", "planbook")]
    if dialect == "postgres":
        assert kinds == [("CreateEnumType", "planstatus"), *tables]
        assert plan["operations"][0]["labels"] == ["draft", "published"]
    else:
        assert kinds == tables
    assert plan["reports"] == []

    rendered = json.loads(
        _plan_from_ir(
            EMPTY_MODELSET, declared, dialect, '{"destructive": false}', render=True
        )
    )
    statements = [sql for op in rendered["operations"] for sql in op["statements"]]
    assert sum("CREATE TYPE" in sql for sql in statements) == (
        1 if dialect == "postgres" else 0
    ), "a type is created once, by its own op"
    creates = [sql for sql in statements if sql.startswith("CREATE TABLE")]
    assert [sql.split('"')[1] for sql in creates] == ["planauthor", "planbook"]


def test_a_rendering_that_leaves_its_op_out_says_so_in_blocks(clean_registry):
    """SQLite cannot make a column ``NOT NULL`` in place: the op renders no
    statement and a report that blocks. ``blocks`` is the core's own word on
    the wire, and it is all the Alembic bridge reads to refuse the op."""
    from ferro import Model, clear_registry
    from ferro.base import FerroField
    from ferro.migrations.alembic import _blocking

    class PlanNote(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        body: str | None = None

    before = _declared_modelset()
    clear_registry()

    class PlanNote(Model):  # noqa: F811 - the same model, edited
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        body: str

    after = _declared_modelset()
    plan = json.loads(
        _plan_from_ir(before, after, "sqlite", '{"destructive": false}', render=True)
    )
    (op,) = plan["operations"]
    assert (op["kind"], op["statements"]) == ("AlterColumnNullability", [])
    assert [(report["kind"], report["blocks"]) for report in op["reports"]] == [
        ({"SqliteInPlace": {"what": "AlterColumnNullability"}}, True)
    ]
    assert _blocking(op) == op["reports"][0]["text"]
    assert _blocking({**op, "reports": [{**op["reports"][0], "blocks": False}]}) is None


def test_plan_from_ir_rejects_what_it_cannot_plan(clean_registry):
    with pytest.raises(ValueError, match="Unknown dialect"):
        _plan_from_ir(EMPTY_MODELSET, EMPTY_MODELSET, "mysql", "{}")
    with pytest.raises(ValueError, match="options_json"):
        _plan_from_ir(EMPTY_MODELSET, EMPTY_MODELSET, "sqlite", '"destructive"')
    with pytest.raises(ValueError, match="ir_kind"):
        not_schema = json.dumps({**json.loads(EMPTY_MODELSET), "ir_kind": "query"})
        _plan_from_ir(not_schema, EMPTY_MODELSET, "sqlite", "{}")


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_freshly_migrated_database_reads_back_as_a_plan_with_nothing_to_do(
    db_url, db_backend, clean_registry
):
    import ferro

    _declare_library_models()
    await ferro.connect(db_url, auto_migrate=True)
    declared = _declared_modelset()

    ir_json, facts_json = await _live_schema_ir(None, declared)
    live = json.loads(ir_json)
    assert [model["table_name"] for model in live["payload"]["models"]] == [
        "planauthor",
        "planbook",
    ]
    for destructive in ("false", "true"):
        plan = json.loads(
            _plan_from_ir(
                ir_json,
                declared,
                db_backend,
                f'{{"destructive": {destructive}}}',
                facts_json=facts_json,
            )
        )
        assert plan == {"operations": [], "reports": []}

    # A table the live database lacks is an add; reading only one table
    # makes the other one missing.
    only_author, author_facts = await _live_schema_ir(
        None, EMPTY_MODELSET, '["planauthor"]'
    )
    plan = json.loads(
        _plan_from_ir(
            only_author,
            declared,
            db_backend,
            '{"destructive": false}',
            facts_json=author_facts,
        )
    )
    assert [op["kind"] for op in plan["operations"]] == ["AddTable"]
    assert plan["operations"][0]["table"] == "planbook"


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_live_facts_carry_check_bodies_validity_and_row_security(
    db_url, db_backend, clean_registry
):
    import ferro

    _declare_library_models()
    await ferro.connect(db_url, auto_migrate=True)
    _, facts_json = await _live_schema_ir(None, _declared_modelset())
    facts = json.loads(facts_json)
    book = facts["tables"]["planbook"]
    assert [check["name"] for check in book["checks"]] == ["ck_planbook_kind"]
    assert book["checks"][0]["ferro_owned"] is True
    assert book["checks"][0]["validated"] is True
    assert all(index["valid"] for index in book["indexes"])
    assert book["row_security"] == {"enabled": False, "forced": False, "policies": []}
    if db_backend == "postgres":
        assert facts["enum_labels"] == {"planstatus": ["draft", "published"]}
        assert book["foreign_keys"] == [
            {"name": "fk_planbook_author_id_planauthor", "validated": True}
        ]
    else:
        assert facts["enum_labels"] == {}
