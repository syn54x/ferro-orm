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
    async with ferro.engines.session():
        # 1. Create a record normally (this WILL call __init__)
        global INIT_CALLED_COUNT
        INIT_CALLED_COUNT = 0
        user = HydrationTestUser(id=1, name="Direct Injector")
        await user.save()
        assert INIT_CALLED_COUNT == 1

    # 2. Reset engine to clear Identity Map (so we force a DB fetch)
    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
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
    async with ferro.engines.session():
        created = SlotCheckUser(id=1, name="slot-check")
        await created.save()

    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
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
    async with ferro.engines.session():
        created = ExtraAllowUser(id=1, name="ea")
        await created.save()

    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
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
    async with ferro.engines.session():
        await SlotPathUser(id=1, name="one").save()

    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        by_get = await SlotPathUser.get(1)
        by_all = (await SlotPathUser.all())[0]
        by_first = await SlotPathUser.where(lambda u: u.id == 1).first()
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
    async with ferro.engines.session():
        await ExtraForbidUser(id=1, name="ef").save()

    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        row = await ExtraForbidUser.get(1)
        assert row is not None
        _assert_pydantic_slots(
            row,
            expected_fields={"id", "name"},
            expected_extra=None,
        )


def test_hydration_abi_guard_names_unknown_slot():
    """FF-G G1: a BaseModel slot the hydrator does not initialize must be a
    loud, actionable error naming the slot and the pydantic version."""
    from pydantic import BaseModel

    from ferro._core import _verify_hydration_abi_for_test

    class FakeBaseModel:
        __slots__ = (*BaseModel.__slots__, "__pydantic_future_slot__")

    with pytest.raises(RuntimeError) as excinfo:
        _verify_hydration_abi_for_test(FakeBaseModel)
    message = str(excinfo.value)
    assert "__pydantic_future_slot__" in message
    assert "pydantic" in message


def test_hydration_abi_guard_passes_real_basemodel():
    """The guard that runs at ferro._core import accepts the installed
    pydantic's real BaseModel (otherwise ferro would refuse to start)."""
    from pydantic import BaseModel

    from ferro._core import _verify_hydration_abi_for_test

    _verify_hydration_abi_for_test(BaseModel)  # must not raise


@pytest.mark.asyncio
async def test_hydration_holds_the_mapped_columns_only(db_url):
    """The root fetch names the registration's mapped columns, never ``*``
    (ADR-0053, the complete-instance invariant): a retired column the table
    still holds, and a column added by hand, never reach the instance, which
    is the one ``__init__`` builds from the same row."""
    from ferro import Field as FerroFieldOption
    from ferro.raw import execute

    class RetiredHydrationUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        name: str
        bio: str | None = FerroFieldOption(retired=True)

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await execute("ALTER TABLE retiredhydrationuser ADD COLUMN extra varchar")
        await execute(
            "INSERT INTO retiredhydrationuser (id, name, bio, extra) "
            "VALUES (1, 'kept', 'old', 'by hand')"
        )

    ferro.reset_engine()
    await ferro.connect(db_url)
    async with ferro.engines.session():
        rows = [
            await RetiredHydrationUser.get(1),
            *(await RetiredHydrationUser.all()),
            await RetiredHydrationUser.where(lambda user: user.id == 1).first(),
        ]

    built = RetiredHydrationUser(id=1, name="kept")
    for row in rows:
        assert row is not None
        row_fields = {k: v for k, v in row.__dict__.items() if not k.startswith("__")}
        assert row_fields == built.__dict__ == {"id": 1, "name": "kept"}
        _assert_pydantic_slots(row, expected_fields={"id", "name"}, expected_extra=None)


@pytest.mark.asyncio
async def test_hydrated_instances_list_fields_in_declaration_order(db_url):
    """A loaded instance lists fields like a built one on every fetch path (I-2)."""
    from typing import Annotated

    from ferro import BackRef, ForeignKey, Relation

    class OrderAuthor(Model):
        id: int | None = Field(default=None, json_schema_extra={"primary_key": True})
        username: str
        email: str
        age: int
        posts: Relation[list["OrderPost"]] = BackRef()

    class OrderPost(Model):
        id: int | None = Field(default=None, json_schema_extra={"primary_key": True})
        title: str
        author: Annotated[OrderAuthor, ForeignKey(related_name="posts")]
        body: str

    declared = ["id", "username", "email", "age"]
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        built = OrderAuthor(id=1, username="ada", email="a@x", age=30)
        await built.save()
        built_keys = list(built.__dict__)
        await OrderPost(id=1, title="t", author=built, body="b").save()

    async with ferro.engines.session():
        got = await OrderAuthor.get(1)
        assert list(got.model_dump()) == declared
        assert list(got.__dict__) == built_keys
        assert list(got.__dict__)[-2:] == [
            "__ferro_connection_name",
            "__ferro_persisted",
        ]
        assert got.__pydantic_fields_set__ == set(declared)

        found = await OrderAuthor.where(lambda author: author.age == 30).all()
        first = await OrderAuthor.where(lambda author: author.age == 30).first()
        for author in (*found, first):
            assert list(author.model_dump()) == declared

        post = (
            await OrderPost.where(lambda post: post.id == 1)
            .include(lambda post: post.author)
            .all()
        )[0]
        assert list(post.author.model_dump()) == declared
        assert list(post.__dict__)[:4] == list(OrderPost.model_fields)[:4]

        for member in await got.posts.all():
            assert list(member.model_dump()) == list(OrderPost.model_fields)


@pytest.mark.asyncio
async def test_identity_refresh_keeps_field_order(db_url):
    class OrderRefresh(Model):
        id: int | None = Field(default=None, json_schema_extra={"primary_key": True})
        username: str
        age: int

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await OrderRefresh(id=1, username="u", age=3).save()
    async with ferro.engines.session():
        held = await OrderRefresh.get(1)
        before = list(held.__dict__)
        again = await OrderRefresh.get(1)
        assert again is held
        assert list(held.__dict__) == before
        assert list(held.model_dump()) == ["id", "username", "age"]


@pytest.mark.asyncio
async def test_hydration_order_with_shadow_column_and_retired_field(db_url):
    """Dump keys follow model_fields with a shadow FK column and a retired field."""
    from typing import Annotated

    from ferro import BackRef, ForeignKey, Relation

    class OrderOwner(Model):
        id: int | None = Field(default=None, json_schema_extra={"primary_key": True})
        rows: Relation[list["OrderRetired"]] = BackRef()

    class OrderRetired(Model):
        id: int | None = Field(default=None, json_schema_extra={"primary_key": True})
        zeta: str
        owner: Annotated[OrderOwner, ForeignKey(related_name="rows")]
        gone: str | None = Field(default=None, retired=True)
        alpha: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        owner = OrderOwner(id=1)
        await owner.save()
        built = OrderRetired(id=1, zeta="z", owner=owner, alpha="a")
        await built.save()
        built_dump = list(built.model_dump())
        built_keys = list(built.__dict__)

    async with ferro.engines.session():
        got = await OrderRetired.get(1)
        assert list(got.model_dump()) == built_dump
        assert list(got.__dict__) == built_keys
