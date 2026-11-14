"""A many-to-many link binds each key as the key column it references.

```python
class Shift(Model):
    day: datetime.date = Field(primary_key=True)
    tags: Relation[list[Tag]] = ManyToMany(through="shift_tags")

await shift.tags.add(tag)
# INSERT INTO "shift_tags" ("shift_id", "tag_id") VALUES (CAST($1 AS date), $2)
```

The key a link statement binds is the value `save()` stored for it, on
either side of the relation and on both dialects.
"""

import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

import pytest

import ferro
from ferro import Field, ManyToMany, Model, Relation
from ferro.raw import fetch_all
from ferro.registry import REGISTRY
from tests._pass_harness import auto_migrate


class Color(str, Enum):
    RED = "red"
    BLUE = "blue"


# Each key: its annotation, the Field options beside primary_key=True, a value.
_KEYS = {
    "Decimal": (Decimal, {}, Decimal("10.50")),
    "date": (datetime.date, {}, datetime.date(2026, 3, 1)),
    "datetime": (
        datetime.datetime,
        {},
        datetime.datetime(2026, 3, 1, 15, 0, tzinfo=datetime.UTC),
    ),
    "time": (datetime.time, {}, datetime.time(15, 0)),
    "UUID": (UUID, {}, UUID("6f1c0f0e-3a52-4b0c-9a51-0d6c2f1e7a11")),
    "Enum": (Color, {}, Color.RED),
    "varchar(36)": (str, {"db_type": "varchar(36)"}, "alpha"),
    "int": (int, {}, 7),
    "float": (float, {}, 1.5),
    "bool": (bool, {}, True),
}

_each_key = pytest.mark.parametrize("key", list(_KEYS))


@pytest.fixture
def fresh():
    ferro.reset_engine()
    ferro.clear_registry()
    REGISTRY.reset_for_test()
    yield
    ferro.reset_engine()
    ferro.clear_registry()
    REGISTRY.reset_for_test()


def _keyed_owner(key):
    """An ``Owner`` keyed by ``key``'s type with ``tags`` through
    ``owner_tags``, its ``Tag``, and a key value."""
    annotation, options, value = _KEYS[key]

    class Tag(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str = ""

    owner = type(
        "Owner",
        (Model,),
        {
            "__module__": __name__,
            "__annotations__": {"key": annotation, "tags": Relation[list[Tag]]},
            "key": Field(primary_key=True, **options),
            "tags": ManyToMany(through="owner_tags"),
        },
    )
    return owner, Tag, value


def _keyed_item(key):
    """An int-keyed ``Owner`` with ``items`` through ``owner_items``, an
    ``Item`` keyed by ``key``'s type, and a key value."""
    annotation, options, value = _KEYS[key]

    item = type(
        "Item",
        (Model,),
        {
            "__module__": __name__,
            "__annotations__": {"key": annotation},
            "key": Field(primary_key=True, **options),
        },
    )

    class Owner(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str = ""
        items: Relation[list[item]] = ManyToMany(through="owner_items")

    return Owner, item, value


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@_each_key
async def test_a_linked_tag_is_read_back_from_a_fetched_owner(db_url, fresh, key):
    Owner, Tag, value = _keyed_owner(key)
    await auto_migrate(db_url)
    async with ferro.engines.session():
        owner = await Owner.create(key=value)
        tag = await Tag.create()
        await owner.tags.add(tag)
    async with ferro.engines.session():
        fetched = await Owner.get(value)

        assert [t.id for t in await fetched.tags.all()] == [tag.id]


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@_each_key
async def test_a_removed_tag_is_unlinked_and_keeps_its_row(db_url, fresh, key):
    Owner, Tag, value = _keyed_owner(key)
    await auto_migrate(db_url)
    async with ferro.engines.session():
        owner = await Owner.create(key=value)
        tag = await Tag.create()
        await owner.tags.add(tag)

        await owner.tags.remove(tag)

        assert await owner.tags.all() == []
        assert (await Tag.get(tag.id)).id == tag.id


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@_each_key
async def test_clear_unlinks_every_tag(db_url, fresh, key):
    Owner, Tag, value = _keyed_owner(key)
    await auto_migrate(db_url)
    async with ferro.engines.session():
        owner = await Owner.create(key=value)
        await owner.tags.add(await Tag.create(), await Tag.create())
        assert len(await owner.tags.all()) == 2

        await owner.tags.clear()

        assert await owner.tags.all() == []


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@_each_key
async def test_a_keyed_related_row_is_linked_read_and_unlinked(db_url, fresh, key):
    Owner, Item, value = _keyed_item(key)
    await auto_migrate(db_url)
    async with ferro.engines.session():
        owner = await Owner.create()
        item = await Item.create(key=value)

        await owner.items.add(item)
        assert [i.key for i in await owner.items.all()] == [value]

        await owner.items.remove(item)
        assert await owner.items.all() == []


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_a_datetime_key_is_linked_as_save_stored_it_on_sqlite(db_url, fresh):
    Owner, Tag, value = _keyed_owner("datetime")
    await auto_migrate(db_url)
    async with ferro.engines.session():
        owner = await Owner.create(key=value)
        tag = await Tag.create()

        await owner.tags.add(tag)

        (stored,) = await fetch_all('SELECT CAST("key" AS TEXT) AS k FROM "owner"')
        (linked,) = await fetch_all(
            'SELECT CAST("owner_id" AS TEXT) AS k FROM "owner_tags"'
        )
        assert linked["k"] == stored["k"]


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_decimal_keyed_relation_counts_its_links_on_postgres(db_url, fresh):
    Owner, Tag, value = _keyed_owner("Decimal")
    await auto_migrate(db_url)
    async with ferro.engines.session():
        owner = await Owner.create(key=value)
        await owner.tags.add(await Tag.create())

        assert await owner.tags.count() == 1
        assert await owner.tags.exists() is True


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_link_statement_logs_the_key_save_logged(
    db_url, fresh, statement_records
):
    Owner, Tag, value = _keyed_owner("date")
    await auto_migrate(db_url)
    async with ferro.engines.session():
        tag = await Tag.create()
        statement_records.clear()
        owner = await Owner.create(key=value)
        await owner.tags.add(tag)

    (saved,) = [r for r in statement_records if r.operation == "save"]
    (added,) = [r for r in statement_records if r.operation == "m2m_add"]
    assert saved.params == ("2026-03-01",)
    assert added.params == ("2026-03-01", tag.id)


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_adding_an_unsaved_tag_is_refused_by_relation_name(db_url, fresh):
    Owner, Tag, value = _keyed_owner("date")
    await auto_migrate(db_url)
    async with ferro.engines.session():
        owner = await Owner.create(key=value)

        with pytest.raises(ValueError, match=r"owner\.tags\.add\(\): Tag\(id=None\)"):
            await owner.tags.add(Tag())


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_an_unsaved_owner_reads_no_links(db_url, fresh):
    Owner, Item, value = _keyed_item("date")
    await auto_migrate(db_url)
    async with ferro.engines.session():
        await (await Owner.create()).items.add(await Item.create(key=value))
        unsaved = Owner()

        assert await unsaved.items.all() == []
        assert await unsaved.items.count() == 0
        assert await unsaved.items.exists() is False


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@pytest.mark.parametrize("verb", ["add", "remove", "clear"])
async def test_an_unsaved_owner_refuses_a_link_write(db_url, fresh, verb):
    Owner, Item, value = _keyed_item("date")
    await auto_migrate(db_url)
    async with ferro.engines.session():
        item = await Item.create(key=value)
        write = getattr(Owner().items, verb)

        with pytest.raises(
            ValueError,
            match=rf"owner\.items\.{verb}\(\): the owner has no primary key yet; "
            "save it first",
        ):
            await write(*(() if verb == "clear" else (item,)))
