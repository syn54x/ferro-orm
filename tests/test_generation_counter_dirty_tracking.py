"""Generation-counter dirty tracking, assemble-not-recompile, compile counter (#245)."""

from typing import Annotated

import pytest

from ferro import Model, connect, reset_engine
from ferro._core import _bulk_install_count_for_test
from ferro.base import FerroField
from ferro.ir.compiler import (
    reset_schema_ir_compile_count_for_test,
    schema_ir_compile_count_for_test,
)
from ferro.registry import REGISTRY


def test_register_and_deregister_bump_generation_counter(clean_registry) -> None:
    assert REGISTRY.registration_generation() == 0
    assert REGISTRY.resolved_generation() == 0
    assert not REGISTRY.is_dirty()

    class GcWidget(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    assert REGISTRY.registration_generation() == 1
    assert REGISTRY.is_dirty()

    REGISTRY.deregister(GcWidget.__ferro_identity__)
    assert REGISTRY.registration_generation() == 2
    assert REGISTRY.is_dirty()


def test_resolve_clears_dirty_generation(clean_registry) -> None:
    from ferro.relations import resolve_relationships

    class GcAuthor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    class GcPost(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author_id: Annotated[int, FerroField(foreign_key="GcAuthor")]

    assert REGISTRY.is_dirty()
    resolve_relationships()
    assert not REGISTRY.is_dirty()
    assert REGISTRY.resolved_generation() == REGISTRY.registration_generation()


def test_assemble_fails_loudly_on_missing_envelope(clean_registry) -> None:
    from ferro.relations import resolve_relationships

    class GcOrphan(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    from ferro.ir.compiler import _assemble_modelset_envelope

    resolve_relationships()
    REGISTRY.evict_envelope(GcOrphan.__ferro_identity__)

    with pytest.raises(RuntimeError, match="Missing SchemaIR envelope"):
        _assemble_modelset_envelope()


@pytest.mark.asyncio
async def test_clean_reconnect_zero_compiles_zero_bulk_install(
    db_url, clean_registry
) -> None:
    """After the first sync, an identical reconnect assembles without recompiling
    and skips the bulk install fingerprint gate."""

    class GcStable(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    reset_schema_ir_compile_count_for_test()
    bulk_baseline = _bulk_install_count_for_test()

    await connect(db_url, auto_migrate=False)
    assert _bulk_install_count_for_test() - bulk_baseline == 1

    reset_engine()
    reset_schema_ir_compile_count_for_test()
    after_first = _bulk_install_count_for_test()

    await connect(db_url, auto_migrate=False)
    assert schema_ir_compile_count_for_test() == 0
    assert _bulk_install_count_for_test() - after_first == 0


@pytest.mark.asyncio
async def test_ensure_rust_registration_synced_routes_connect(
    db_url, clean_registry
) -> None:
    from ferro import ensure_resolved_modelset

    class GcRouted(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        value: str

    await connect(db_url, auto_migrate=True)
    reset_schema_ir_compile_count_for_test()
    modelset = ensure_resolved_modelset()
    assert schema_ir_compile_count_for_test() == 0
    cached = REGISTRY.modelset()
    assert cached is not None
    assert modelset is cached[0]
