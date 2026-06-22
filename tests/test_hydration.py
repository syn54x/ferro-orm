import pytest
from pydantic import ConfigDict, Field

import ferro
from ferro import Model

pytestmark = pytest.mark.backend_matrix


INIT_CALLED_COUNT = 0


def _assert_pydantic_slots(
    row: Model,
    *,
    expected_fields: set[str],
    expected_extra: dict | None,
) -> None:
    assert row.__pydantic_fields_set__ == expected_fields
    assert row.__pydantic_extra__ == expected_extra
    assert row.__pydantic_private__ is None


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
    _assert_pydantic_slots(
        row,
        expected_fields={"id", "name"},
        expected_extra=None,
    )
    assert dict(row)["name"] == "slot-check"
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
    _assert_pydantic_slots(
        row,
        expected_fields={"id", "name"},
        expected_extra={},
    )
    assert dict(row)["name"] == "ea"


@pytest.mark.asyncio
async def test_hydration_slots_match_across_get_all_and_first(db_url):
    class SlotPathUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        name: str

    await ferro.connect(db_url, auto_migrate=True)
    await SlotPathUser(id=1, name="one").save()

    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)

    by_get = await SlotPathUser.get(1)
    by_all = (await SlotPathUser.all())[0]
    by_first = await SlotPathUser.where(SlotPathUser.id == 1).first()
    assert by_get is not None
    assert by_first is not None

    for row in (by_get, by_all, by_first):
        _assert_pydantic_slots(
            row,
            expected_fields={"id", "name"},
            expected_extra=None,
        )
        assert row.name == "one"


@pytest.mark.asyncio
async def test_hydrated_extra_forbid_initializes_slots(db_url):
    class ExtraForbidUser(Model):
        model_config = ConfigDict(extra="forbid")

        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        name: str

    await ferro.connect(db_url, auto_migrate=True)
    await ExtraForbidUser(id=1, name="ef").save()

    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)

    row = await ExtraForbidUser.get(1)
    assert row is not None
    _assert_pydantic_slots(
        row,
        expected_fields={"id", "name"},
        expected_extra=None,
    )
