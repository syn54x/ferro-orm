"""Provisional import: class-body registration is Python-only (#246).

Import-time registration writes Python caches and bumps the generation counter
only. The Rust column registry stays empty until the first bulk install at
``connect()``/``create_tables()``/``migrate()``.
"""

import ast
from pathlib import Path
from typing import Annotated, ClassVar
from uuid import UUID, uuid4

import pytest

import ferro
from ferro import (
    FerroField,
    Model,
    Relation,
    clear_registry,
    connect,
    reset_engine,
)
from ferro._core import (
    _bulk_install_count_for_test,
    _rust_model_registry_count_for_test,
    register_model_schema,
)
from ferro.base import ForeignKey
from ferro.fields import BackRef
from ferro.ir.compiler import (
    reset_schema_ir_compile_count_for_test,
    schema_ir_compile_count_for_test,
)
from ferro import state as ferro_state

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "ferro"


class _ColdPiRow(Model):
    """Module-level model for cold-rehydration after clear_registry (#246)."""

    __ferro_table__: ClassVar[str] = "pi_cold_rows"
    id: Annotated[UUID | None, FerroField(primary_key=True)] = None
    label: str


@pytest.fixture(autouse=True)
def _cold_pi_row_survives_registry_wipe():
    """Keep the module-level cold-rehydration model registered across wipes."""
    if _ColdPiRow.__ferro_identity__ not in ferro_state._MODEL_REGISTRY_PY:
        ferro_state.register_model(_ColdPiRow)
    yield


def test_class_body_does_not_call_register_model_schema(
    clean_registry, monkeypatch
) -> None:
    """Metaclass registration must not push schema to Rust at import time."""

    calls: list[tuple[object, ...]] = []

    def tracking(name: str, schema: str, table_name: str) -> None:
        calls.append((name, schema, table_name))

    monkeypatch.setattr("ferro._core.register_model_schema", tracking)

    class PiWidget(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    assert calls == []


def test_rust_registry_empty_after_import_before_connect(clean_registry) -> None:
    """After class definition, Python caches are warm but Rust registry is empty."""

    class PiFresh(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        note: str

    identity = PiFresh.__ferro_identity__
    assert ferro_state._MODEL_REGISTRY_PY[identity] is PiFresh
    assert identity in ferro_state._SCHEMA_IR_BY_MODEL
    assert ferro_state.registration_generation() == 1
    assert _rust_model_registry_count_for_test() == 0


@pytest.mark.asyncio
async def test_connect_populates_via_single_bulk_install(
    db_url, clean_registry
) -> None:
    """First connect performs exactly one bulk install; queries work."""

    class PiConnect(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        value: str

    assert _rust_model_registry_count_for_test() == 0
    baseline = _bulk_install_count_for_test()

    await connect(db_url, auto_migrate=True)
    assert _bulk_install_count_for_test() - baseline == 1
    assert _rust_model_registry_count_for_test() == 1

    async with ferro.engines.session():
        row = await PiConnect.create(value="ok")
        assert (await PiConnect.get(row.id)).value == "ok"


@pytest.mark.asyncio
async def test_clear_registry_connect_one_bulk_no_recompile(
    db_url, clean_registry
) -> None:
    """clear_registry + connect: one bulk push, assemble-only, no recompile."""

    class PiRepush(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        tag: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await PiRepush.create(tag="seed")

    reset_engine()
    clear_registry()
    assert _rust_model_registry_count_for_test() == 0

    reset_schema_ir_compile_count_for_test()
    bulk_after_clear = _bulk_install_count_for_test()

    await connect(db_url, auto_migrate=True)
    assert _bulk_install_count_for_test() - bulk_after_clear == 1
    assert schema_ir_compile_count_for_test() == 0
    assert _rust_model_registry_count_for_test() == 1

    async with ferro.engines.session():
        rows = await PiRepush.all()
        assert len(rows) == 1
        assert rows[0].tag == "seed"


@pytest.mark.asyncio
async def test_cold_rehydration_after_clear_registry(db_url) -> None:
    """Module-level models survive clear_registry + connect (#246 / #65 pattern)."""
    reset_engine()
    clear_registry()
    assert _ColdPiRow.__ferro_identity__ in ferro_state._MODEL_REGISTRY_PY

    await connect(db_url, auto_migrate=True)
    row_id = uuid4()
    async with ferro.engines.session():
        await _ColdPiRow.create(id=row_id, label="cold")

    reset_engine()
    clear_registry()
    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        loaded = (await _ColdPiRow.get(row_id)).label
    assert loaded == "cold"


@pytest.mark.asyncio
async def test_relationship_models_stay_python_only_until_connect(
    db_url, clean_registry
) -> None:
    """FK + BackRef resolution at import still does not populate Rust."""

    class PiAuthor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        posts: Relation[list["PiPost"]] = BackRef()

    class PiPost(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author: Annotated["PiAuthor", ForeignKey(related_name="posts")]

    from ferro.relations import resolve_relationships

    resolve_relationships()
    assert _rust_model_registry_count_for_test() == 0
    assert ferro_state._MODEL_REGISTRY_PY[PiAuthor.__ferro_identity__] is PiAuthor
    assert ferro_state._MODEL_REGISTRY_PY[PiPost.__ferro_identity__] is PiPost

    baseline = _bulk_install_count_for_test()
    await connect(db_url, auto_migrate=True)
    assert _bulk_install_count_for_test() - baseline == 1
    assert _rust_model_registry_count_for_test() == 2


def _register_model_schema_call_linenos(source: str) -> list[int]:
    tree = ast.parse(source)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "register_model_schema":
            hits.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == "register_model_schema":
            hits.append(node.lineno)
    return sorted(set(hits))


def test_no_register_model_schema_calls_in_ferro_package() -> None:
    """Python registration paths must not call the legacy per-model Rust FFI."""
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        for lineno in _register_model_schema_call_linenos(path.read_text(encoding="utf-8")):
            rel = path.relative_to(_SRC_ROOT)
            offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "register_model_schema is legacy per-model Rust registration; "
        "use Python envelope caches + bulk install instead:\n"
        + "\n".join(offenders)
    )


def test_register_model_schema_still_available_for_direct_tests() -> None:
    """Sanity: the FFI symbol exists (tests may call it explicitly)."""
    assert callable(register_model_schema)
