"""Bulk atomic install FFI + fingerprint gate + push counter (#244).

Registration state (the Rust column registry, the schema modelset, and the
recorded modelset fingerprint) is installed through exactly one atomic bulk
entrypoint. These tests drive the behavior through the public Python surface
(``connect``/``create_tables``) plus the ``_bulk_install_count_for_test``
instrument that mirrors the ``_catalog_query_count_for_test`` precedent:

- a first connect performs exactly one bulk install;
- an identical reconnect is gated by the fingerprint and installs zero times;
- a model defined after connect changes the fingerprint and installs again;
- a failed/invalid install leaves the previous registration operable and
  unchanged (build-then-swap, retained-last-good).
"""

import json
from typing import Annotated

import pytest

import ferro
from ferro import Model, connect, create_tables, reset_engine
from ferro._core import _bulk_install_count_for_test, _install_registration
from ferro.base import FerroField

# ``clean_registry`` is the shared fixture from ``tests/conftest.py`` (#243) —
# it wipes every per-model store before and after the test.


@pytest.mark.asyncio
async def test_first_connect_performs_single_bulk_install(db_url, clean_registry):
    """A first connect installs the registration exactly once, and the created
    schema is usable."""

    class BiWidget(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    baseline = _bulk_install_count_for_test()
    await connect(db_url, auto_migrate=True)
    assert _bulk_install_count_for_test() - baseline == 1

    async with ferro.engines.session():
        row = await BiWidget.create(name="hello")
        assert (await BiWidget.get(row.id)).name == "hello"


@pytest.mark.asyncio
async def test_identical_reconnect_skips_install(db_url, clean_registry):
    """Reconnecting with no model changes is gated by the fingerprint: the bulk
    install runs zero times the second time."""

    class BiFixed(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    baseline = _bulk_install_count_for_test()
    await connect(db_url, auto_migrate=False)
    assert _bulk_install_count_for_test() - baseline == 1

    reset_engine()

    after_first = _bulk_install_count_for_test()
    await connect(db_url, auto_migrate=False)
    assert _bulk_install_count_for_test() - after_first == 0


@pytest.mark.asyncio
async def test_model_defined_after_connect_reinstalls(db_url, clean_registry):
    """A model declared after connect changes the modelset fingerprint, so the
    next sync (here ``create_tables``) performs a fresh bulk install and the new
    table is created."""

    class BiEarly(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)
    after_connect = _bulk_install_count_for_test()

    class BiLate(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        note: str

    await create_tables()
    assert _bulk_install_count_for_test() - after_connect == 1

    async with ferro.engines.session():
        row = await BiLate.create(note="late")
        assert (await BiLate.get(row.id)).note == "late"


def _uncompilable_payload() -> str:
    """A structurally valid schema envelope whose sole column has an unknown
    ``logical_type`` — it parses, but fails to compile a codec plan, so the
    install must abort during the build phase (before any swap)."""
    return json.dumps(
        {
            "ir_kind": "schema",
            "ir_version": 1,
            "payload": {
                "dialect_agnostic": True,
                "models": [
                    {
                        "model_name": "BiBroken",
                        "table_name": "bibroken",
                        "columns": [
                            {
                                "name": "x",
                                "logical_type": "not_a_real_type",
                                "nullable": True,
                                "primary_key": False,
                                "autoincrement": False,
                                "unique": False,
                                "index": False,
                                "default": None,
                                "format": None,
                            }
                        ],
                        "foreign_keys": [],
                        "indexes": [],
                        "uniques": [],
                        "checks": [],
                    }
                ],
            },
        }
    )


@pytest.mark.asyncio
async def test_failed_install_retains_previous_registration(db_url, clean_registry):
    """Build-then-swap: a failed install leaves the previously installed
    registration and its fingerprint operable and unchanged."""

    class BiGood(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        row = await BiGood.create(name="keep")

    installs_before = _bulk_install_count_for_test()

    # An invalid install with a *new* fingerprint must raise and must not
    # mutate any store or bump the counter.
    with pytest.raises(ValueError):
        _install_registration(_uncompilable_payload(), "fingerprint-that-differs")

    assert _bulk_install_count_for_test() == installs_before

    # The previous registration is still fully operable.
    async with ferro.engines.session():
        assert (await BiGood.get(row.id)).name == "keep"

    # And the recorded fingerprint is still BiGood's: re-pushing the unchanged
    # modelset is gated (zero further installs).
    ferro._push_registration_to_rust()
    assert _bulk_install_count_for_test() == installs_before
