"""The field type tables in ``docs/includes/field-types.md`` are generated.

Each cell is what the create pass writes for one column on one dialect: the
type ``ferro_ddl_lowering::resolve_column_storage`` decides (read here over
FFI as ``_core._resolve_storage_type``, AGENTS.md I-1 item 3), rendered by
the same create pass ``connect(auto_migrate=True)`` runs. A hand edit to the
include, or a change to the storage decision, fails
``test_field_type_tables_are_current`` until the include is regenerated:

    python -m tests.test_docs_field_type_table
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from ferro import Field, Model, clear_registry, reset_engine, varchar
from ferro._core import _render_create_table_sql_for_test, _resolve_storage_type
from ferro.columns import build_column_specs
from ferro.ir.compiler import compile_schema_ir_payload

INCLUDE = Path(__file__).resolve().parents[1] / "docs" / "includes" / "field-types.md"
DIALECTS = (("sqlite", "SQLite"), ("postgres", "PostgreSQL"))


class Status(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class Priority(IntEnum):
    LOW = 1
    NORMAL = 2
    HIGH = 3


class Dimensions(BaseModel):
    width_mm: int
    height_mm: int


# (column name, annotation as the table prints it, annotation, Field() options)
ANNOTATION_ROWS: list[tuple[str, str, Any, dict[str, Any]]] = [
    ("id", "`int` primary key", int | None, {"default": None, "primary_key": True}),
    ("quantity", "`int`", int, {}),
    ("name", "`str`", str, {}),
    ("in_stock", "`bool`", bool, {}),
    ("weight", "`float`", float, {}),
    ("price", "`decimal.Decimal`", Decimal, {}),
    ("thumbnail", "`bytes`", bytes, {}),
    ("created_at", "`datetime.datetime`", dt.datetime, {}),
    ("released_on", "`datetime.date`", dt.date, {}),
    ("opens_at", "`datetime.time`", dt.time, {}),
    ("token", "`uuid.UUID`", UUID, {}),
    ("status", "`Status`, a `StrEnum` (`draft`, `active`, `retired`)", Status, {}),
    ("priority", "`Priority`, an `IntEnum` (`1`, `2`, `3`)", Priority, {}),
    ("size", '`Literal["S", "M", "L"]`', Literal["S", "M", "L"], {}),
    ("attributes", "`dict[str, str]`", dict[str, str], {}),
    ("tags", "`list[str]`", list[str], {}),
    ("dimensions", "`Dimensions`, a Pydantic `BaseModel`", Dimensions, {}),
]

# (db_type token as the table prints it, annotation, the token)
TOKEN_ROWS: list[tuple[str, Any, str]] = [
    ('`"text"`', str, "text"),
    ("`varchar(255)`", str, varchar(255)),
    ('`"smallint"`', int, "smallint"),
    ('`"int"`', int, "int"),
    ('`"bigint"`', int, "bigint"),
    ('`"uuid"`', UUID, "uuid"),
    ('`"timestamp"`', dt.datetime, "timestamp"),
    ('`"timestamptz"`', dt.datetime, "timestamptz"),
    ('`"date"`', dt.date, "date"),
    ('`"time"`', dt.time, "time"),
    ('`"json"`', dict[str, str], "json"),
    ('`"jsonb"`', dict[str, str], "jsonb"),
]


@contextmanager
def _empty_registry() -> Iterator[None]:
    from ferro.registry import REGISTRY

    def reset() -> None:
        REGISTRY.reset_for_test()
        reset_engine()
        clear_registry()

    reset()
    try:
        yield
    finally:
        reset()


def _column_definitions(
    name: str, columns: list[tuple[str, Any, dict[str, Any]]]
) -> dict[str, dict[str, str]]:
    """Each column's definition after its name, per dialect, ``NOT NULL`` left out.

    The model is compiled to SchemaIR and rendered by the create pass, the
    one each dialect's ``connect(auto_migrate=True)`` runs. A native enum
    column is named by its type, as ``_resolve_storage_type`` decides it.
    """
    annotations = {column: annotation for column, annotation, _ in columns}
    namespace: dict[str, Any] = {
        "__annotations__": annotations,
        "__module__": __name__,
        **{column: Field(**options) for column, _, options in columns if options},
    }
    with _empty_registry():
        model = type(name, (Model,), namespace)
        specs = build_column_specs(model)
    payload = compile_schema_ir_payload(
        name, list(specs.values()), table_name=name.lower()
    )
    [ir_model] = payload["models"]
    definitions: dict[str, dict[str, str]] = {column: {} for column in annotations}
    for dialect, _ in DIALECTS:
        create, _, _ = _render_create_table_sql_for_test(
            name, json.dumps(payload), dialect
        )
        for column in ir_model["columns"]:
            storage = json.loads(_resolve_storage_type(json.dumps(column), dialect))
            if storage["kind"] == "pg_enum":
                cell = f"native enum type `{storage['name']}`"
            else:
                cell = f"`{_definition(create, column['name'])}`"
            definitions[column["name"]][dialect] = cell
    return definitions


def _definition(create: str, column: str) -> str:
    match = re.search(rf'"{column}" ([^,]+?)\s*(?:,|\s\)$)', create)
    if match is None:
        raise AssertionError(f"column {column!r} not found in {create}")
    return re.sub(r"\s*\bNOT NULL\b", "", match.group(1)).strip()


def _table(header: str, rows: list[tuple[str, dict[str, str]]]) -> list[str]:
    lines = [
        f"| {header} | {' | '.join(label for _, label in DIALECTS)} |",
        "| :--- | :--- | :--- |",
    ]
    for label, cells in rows:
        row = " | ".join(cells[dialect] for dialect, _ in DIALECTS)
        lines.append(f"| {label} | {row} |")
    return lines


def render_include() -> str:
    """The include's full text, regenerated from the create pass."""
    annotation_cells = _column_definitions(
        "FieldTypes",
        [
            (column, annotation, options)
            for column, _, annotation, options in ANNOTATION_ROWS
        ],
    )
    token_columns = [f"c{index}" for index in range(len(TOKEN_ROWS))]
    token_cells = _column_definitions(
        "DbTypeTokens",
        [
            (column, annotation, {"db_type": token})
            for column, (_, annotation, token) in zip(token_columns, TOKEN_ROWS)
        ],
    )
    lines = [
        "<!-- Generated by `python -m tests.test_docs_field_type_table`; do not edit. -->",
        "",
        "<!-- --8<-- [start:annotations] -->",
        *_table(
            "Annotation",
            [
                (label, annotation_cells[column])
                for column, label, _, _ in ANNOTATION_ROWS
            ],
        ),
        "<!-- --8<-- [end:annotations] -->",
        "",
        "<!-- --8<-- [start:db-type-tokens] -->",
        *_table(
            "`db_type`",
            [
                (label, token_cells[column])
                for column, (label, _, _) in zip(token_columns, TOKEN_ROWS)
            ],
        ),
        "<!-- --8<-- [end:db-type-tokens] -->",
    ]
    return "\n".join(lines) + "\n"


def test_field_type_tables_are_current() -> None:
    assert INCLUDE.read_text(encoding="utf-8") == render_include(), (
        f"{INCLUDE.relative_to(INCLUDE.parents[2])} is out of date; regenerate it "
        "with `python -m tests.test_docs_field_type_table`"
    )


def test_a_cell_is_the_create_pass_column_type() -> None:
    cells = _column_definitions("Probe", [("name", str, {})])
    assert cells["name"] == {"sqlite": "`varchar`", "postgres": "`varchar`"}


if __name__ == "__main__":
    INCLUDE.write_text(render_include(), encoding="utf-8")
    print(f"wrote {INCLUDE}", file=sys.stderr)
