import pytest
from pydantic import ConfigDict, Field

import ferro
from ferro import Model

pytestmark = pytest.mark.backend_matrix


INIT_CALLED_COUNT = 0


@pytest.mark.asyncio
async def test_direct_injection_bypasses_init(db_url):
    """
    Test that Ferro's Direct Injection bypasses the Python __init__ method.
    """

    # Use a unique class name for this test to avoid registry issues
    class HydrationTestUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        name: str

        def __init__(self, **data):
            super().__init__(**data)
            global INIT_CALLED_COUNT
            INIT_CALLED_COUNT += 1

    await ferro.connect(db_url, auto_migrate=True)

    # 1. Create a record normally (this WILL call __init__)
    global INIT_CALLED_COUNT
    INIT_CALLED_COUNT = 0
    user = HydrationTestUser(id=1, name="Direct Injector")
    await user.save()
    assert INIT_CALLED_COUNT == 1

    # 2. Reset engine to clear Identity Map (so we force a DB fetch)
    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)

    # 3. Fetch the record
    INIT_CALLED_COUNT = 0
    fetched_user = await HydrationTestUser.get(1)

    assert fetched_user is not None
    assert fetched_user.name == "Direct Injector"

    # CRITICAL ASSERTION: If Direct Injection is working, __init__ was never called
    # by the Rust core when instantiating this object.
    assert INIT_CALLED_COUNT == 0


@pytest.mark.asyncio
async def test_hydrated_row_initializes_pydantic_slots(db_url):
    """Rust-hydrated instances must match __init__ for Pydantic slot attributes."""

    class SlotCheckUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        name: str

    await ferro.connect(db_url, auto_migrate=True)
    created = SlotCheckUser(id=1, name="slot-check")
    await created.save()

    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)

    row = await SlotCheckUser.get(1)
    assert row is not None
    assert dict(row)["name"] == "slot-check"
    assert row.__pydantic_extra__ is None
    assert row.__pydantic_private__ is None
    copied = row.model_copy()
    assert copied.name == row.name
    assert dict(copied)["name"] == "slot-check"


@pytest.mark.asyncio
async def test_hydrated_extra_allow_starts_with_empty_extra_dict(db_url):
    """When extra='allow', __pydantic_extra__ is {} even when no unknown keys exist."""

    class ExtraAllowUser(Model):
        model_config = ConfigDict(extra="allow")

        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        name: str

    await ferro.connect(db_url, auto_migrate=True)
    created = ExtraAllowUser(id=1, name="ea")
    await created.save()

    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)

    row = await ExtraAllowUser.get(1)
    assert row is not None
    assert row.__pydantic_extra__ == {}
    assert row.__pydantic_private__ is None
    assert dict(row)["name"] == "ea"
