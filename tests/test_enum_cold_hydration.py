"""Regression for #65: Annotated StrEnum fields hydrate as enums after cold fetch."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

import pytest

from ferro import FerroField, Model, clear_registry, connect, engines, reset_engine
from ferro.registry import REGISTRY

pytestmark = pytest.mark.backend_matrix


class BillingMode(StrEnum):
    HOURLY = "hourly"


class BillingRow(Model):
    id: Annotated[UUID | None, FerroField(primary_key=True)] = None
    name: str
    billing_mode: Annotated[BillingMode, FerroField(db_type="text")]


@pytest.fixture(autouse=True)
def cleanup():
    registered_before = set(REGISTRY.models())
    # Other tests (e.g. test_sqlite_alembic_reconnect_hydration) clear the Python registry.
    if BillingRow.__ferro_identity__ not in REGISTRY.models():
        REGISTRY.register(BillingRow)
    reset_engine()
    clear_registry()
    yield
    reset_engine()
    clear_registry()
    for name in set(REGISTRY.models()) - registered_before:
        REGISTRY.deregister(name)


def test_enum_fields_populated_for_deferred_annotations():
    """Class definition must register enum fields before any fetch (#65)."""
    assert BillingRow._enum_fields == {"billing_mode": BillingMode}


def test_enum_type_name_unchanged_for_deferred_annotated_strenum():
    assert BillingRow.__ferro_columns__["billing_mode"].enum_type_name == "billingmode"


@pytest.mark.asyncio
async def test_annotated_strenum_text_cold_fetch_after_reset_engine(db_url):
    """Cold read after reset_engine must return StrEnum members, not str (#65)."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():

        row_id = uuid4()
        await BillingRow.create(id=row_id, name="x", billing_mode=BillingMode.HOURLY)

    reset_engine()
    await connect(db_url, auto_migrate=True)
    async with engines.session():

        loaded = (await BillingRow.all())[0]
        assert isinstance(loaded.billing_mode, BillingMode)
        assert loaded.billing_mode == BillingMode.HOURLY
        assert loaded.billing_mode.value == "hourly"
