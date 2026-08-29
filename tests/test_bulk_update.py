import json
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest

from ferro import Field, FerroField, Model, connect, engines, now
from ferro._core import update_filtered
from ferro.query.wire import compile_query, model_identity
from ferro.state import resolve_operation_scope

pytestmark = pytest.mark.backend_matrix


@pytest.mark.asyncio
async def test_bulk_update_operation(db_url):
    """Test that .update(**kwargs) correctly modifies multiple records."""

    class BulkProduct(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        in_stock: bool
        category: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await BulkProduct(name="P1", in_stock=True, category="Electronics").save()
        await BulkProduct(name="P2", in_stock=True, category="Electronics").save()
        await BulkProduct(name="P3", in_stock=True, category="Furniture").save()

        # Bulk update: set in_stock=False for all Electronics
        updated_count = await BulkProduct.where(
            lambda p: p.category == "Electronics"
        ).update(in_stock=False)
        assert updated_count == 2

        # Verify results
        electronics = await BulkProduct.where(
            lambda p: p.category == "Electronics"
        ).all()
        assert all(p.in_stock is False for p in electronics)

        furniture = await BulkProduct.where(lambda p: p.category == "Furniture").all()
        assert furniture[0].in_stock is True


@pytest.mark.asyncio
async def test_bulk_update_evicts_identity_map(db_url):
    """Test that bulk update evicts objects from the Identity Map to prevent stale data."""

    class BulkProduct(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        price: float

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        p1 = BulkProduct(name="Gadget", price=10.0)
        await p1.save()

        # Ensure it's in Identity Map
        cached_p1 = await BulkProduct.get(p1.id)
        assert cached_p1 is p1

        # Update price via bulk query
        await BulkProduct.where(lambda p: p.id == p1.id).update(price=20.0)

        # Fetching again should NOT return the old 'p1' object (it should be a fresh object or re-hydrated)
        fresh_p1 = await BulkProduct.get(p1.id)
        assert fresh_p1 is not p1
        assert fresh_p1.price == 20.0


@pytest.mark.asyncio
async def test_bulk_update_rejects_integer_outside_native_bind_range(db_url):
    class Counter(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        value: int

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        counter = Counter(value=1)
        await counter.save()

        with pytest.raises(ValueError, match="int|number out of range"):
            await Counter.where(lambda row: row.id == counter.id).update(value=2**100)

        fresh = await Counter.get(counter.id)
        assert fresh.value == 1


@pytest.mark.asyncio
async def test_bulk_update_rejects_query_ir_model_identity_mismatch(db_url):
    class Source(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        value: str

    class Other(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        value: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        source = Source(value="original")
        await source.save()

        envelope = json.loads(
            compile_query(
                Source.where(lambda row: row.id == source.id),
                "update",
                assignments={"value": "mutated"},
            ).wire_json
        )
        source_identity = model_identity(Source)
        other_identity = model_identity(Other)
        envelope["payload"]["model_name"] = other_identity
        route = resolve_operation_scope(using=None, session=None)

        with pytest.raises(ValueError) as exc_info:
            await update_filtered(
                source_identity,
                json.dumps(envelope, separators=(",", ":")),
                route,
            )

        message = str(exc_info.value)
        assert source_identity in message
        assert other_identity in message
        assert (await Source.get(source.id)).value == "original"


@pytest.mark.asyncio
async def test_recipe_mixed_literal_and_column_copy(db_url):
    """Recipe door: literal + root column copy persist on both backends (#377)."""

    class User(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: str
        active: bool
        score: int
        bonus: int

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await User(email="a@ferro.dev", active=True, score=10, bonus=1).save()
        await User(email="b@ferro.dev", active=True, score=20, bonus=2).save()
        await User(email="c@ferro.dev", active=False, score=30, bonus=3).save()

        updated = await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {
                "email": "updated@ferro.dev",
                "bonus": user.score,
            }
        )
        assert updated == 2

        rows = await User.where(lambda user: user.active == True).all()  # noqa: E712
        assert {row.email for row in rows} == {"updated@ferro.dev"}
        assert {row.bonus for row in rows} == {10, 20}

        inactive = await User.where(lambda user: user.active == False).first()  # noqa: E712
        assert inactive is not None
        assert inactive.email == "c@ferro.dev"
        assert inactive.bonus == 3


@pytest.mark.asyncio
async def test_recipe_update_evicts_identity_map(db_url):
    """Recipe-door update reuses update_filtered eviction (#377)."""

    class User(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: str
        score: int
        bonus: int

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        user = User(email="cached@ferro.dev", score=7, bonus=1)
        await user.save()

        cached = await User.get(user.id)
        assert cached is user

        updated = await User.where(lambda row: row.id == user.id).update(
            lambda row: {
                "email": "updated@ferro.dev",
                "bonus": row.score,
            }
        )
        assert updated == 1

        fresh = await User.get(user.id)
        assert fresh is not user
        assert fresh.email == "updated@ferro.dev"
        assert fresh.bonus == 7


@pytest.mark.asyncio
async def test_recipe_increment_persists(db_url):
    """``counter.n + 1`` writes ``n = n + 1`` on both backends (#378)."""

    class Counter(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        n: int = 0

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        row = Counter(n=10)
        await row.save()
        cid = row.id

        updated = await Counter.where(lambda counter: counter.id == cid).update(
            lambda counter: {"n": counter.n + 1}
        )
        assert updated == 1
        assert (await Counter.get(cid)).n == 11

        updated = await Counter.where(lambda counter: counter.id == cid).update(
            lambda counter: {"n": counter.n - 1}
        )
        assert updated == 1
        assert (await Counter.get(cid)).n == 10


@pytest.mark.asyncio
async def test_recipe_column_plus_column_persists(db_url):
    """``counter.a + counter.b`` writes the sum on both backends (#378)."""

    class Counter(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        a: int = 0
        b: int = 0
        n: int = 0

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        row = Counter(a=3, b=4, n=0)
        await row.save()
        cid = row.id

        updated = await Counter.where(lambda counter: counter.id == cid).update(
            lambda counter: {"n": counter.a + counter.b}
        )
        assert updated == 1
        assert (await Counter.get(cid)).n == 7


@pytest.mark.asyncio
async def test_recipe_null_plus_one_stays_null(db_url):
    """Honest SQL NULL: ``NULL + 1`` is NULL — no COALESCE (#378)."""

    class Counter(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        n: int | None = None

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        row = Counter(n=None)
        await row.save()
        cid = row.id

        updated = await Counter.where(lambda counter: counter.id == cid).update(
            lambda counter: {"n": counter.n + 1}
        )
        assert updated == 1
        assert (await Counter.get(cid)).n is None


@pytest.mark.asyncio
async def test_recipe_now_writes_database_clock(db_url):
    """``now`` is the DB clock on a datetime column, not a compile-time Python datetime (#378)."""

    class Stamp(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        updated_at: datetime | None = None

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        old = datetime(2000, 1, 1, tzinfo=UTC)
        row = Stamp(updated_at=old)
        await row.save()
        sid = row.id

        before = datetime.now(UTC) - timedelta(seconds=5)
        updated = await Stamp.where(lambda stamp: stamp.id == sid).update(
            lambda stamp: {"updated_at": now}
        )
        assert updated == 1

        fresh = await Stamp.get(sid)
        assert fresh.updated_at is not None
        assert fresh.updated_at != old
        written = fresh.updated_at
        if written.tzinfo is None:
            written = written.replace(tzinfo=UTC)
        assert written >= before


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_recipe_merge_shallow_and_null_object(db_url):
    """Postgres-only shallow merge; NULL object column becomes the patch (#379)."""

    class Conversation(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        turns: dict | None = None

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        filled = Conversation(turns={"keep": 1, "overlap": "old"})
        await filled.save()
        empty = Conversation(turns=None)
        await empty.save()
        fid, eid = filled.id, empty.id

        updated = await Conversation.where(
            lambda conversation: conversation.id == fid
        ).update(
            lambda conversation: {
                "turns": conversation.turns.merge({"overlap": "new", "added": 2})
            }
        )
        assert updated == 1
        fresh = await Conversation.get(fid)
        assert fresh.turns == {"keep": 1, "overlap": "new", "added": 2}

        updated = await Conversation.where(
            lambda conversation: conversation.id == eid
        ).update(
            lambda conversation: {
                "turns": conversation.turns.merge({"run": {"ok": True}})
            }
        )
        assert updated == 1
        assert (await Conversation.get(eid)).turns == {"run": {"ok": True}}


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_recipe_merge_json_column_persists(db_url):
    """db_type=json (ADR-0005 opt-out) still shallow-merges via jsonb ||."""

    class Note(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        payload: dict = Field(default_factory=dict, db_type="json")

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        row = Note(payload={"keep": 1, "overlap": "old"})
        await row.save()
        nid = row.id

        updated = await Note.where(lambda note: note.id == nid).update(
            lambda note: {"payload": note.payload.merge({"overlap": "new", "added": 2})}
        )
        assert updated == 1
        fresh = await Note.get(nid)
        assert fresh.payload == {"keep": 1, "overlap": "new", "added": 2}


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_recipe_merge_evicts_identity_map(db_url):
    """Merge updates reuse the existing update() eviction / rowcount contract."""

    class Conversation(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        turns: dict = Field(default_factory=dict)

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        row = Conversation(turns={"a": 1})
        await row.save()
        cid = row.id

        cached = await Conversation.get(cid)
        assert cached is row

        updated = await Conversation.where(
            lambda conversation: conversation.id == cid
        ).update(lambda conversation: {"turns": conversation.turns.merge({"b": 2})})
        assert updated == 1

        fresh = await Conversation.get(cid)
        assert fresh is not row
        assert fresh.turns == {"a": 1, "b": 2}
