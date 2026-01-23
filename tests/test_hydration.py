import os
import uuid

import pytest
from pydantic import Field

import ferro
from ferro import Model


@pytest.fixture
def db_url():
    db_file = f"test_hydration_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)


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
