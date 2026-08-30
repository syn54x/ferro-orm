"""FF-F F-1 exit-gate tests: the builder never mutates in place."""

from typing import Annotated

import pytest

import ferro
from ferro import FerroField, Model
from ferro.query import Query, Relation
from ferro.query.wire import OrderByEntry


pytestmark = pytest.mark.sqlite_only


class TestImmutableChaining:
    def test_where_returns_new_query_and_leaves_original_unchanged(self):
        class ImmUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        q1 = Query(ImmUser)
        q2 = q1.where(lambda u: u.age >= 18)
        assert q2 is not q1
        assert q1.where_clause == []
        assert len(q2.where_clause) == 1

    def test_chained_where_does_not_alias_where_clause_list(self):
        class ImmUser2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0
            name: str = ""

        q1 = Query(ImmUser2).where(lambda u: u.age >= 18)
        q2 = q1.where(lambda u: u.name == "x")
        assert q1.where_clause is not q2.where_clause
        assert len(q1.where_clause) == 1
        assert len(q2.where_clause) == 2

    def test_limit_offset_order_by_do_not_mutate(self):
        class ImmUser3(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        q1 = Query(ImmUser3)
        q2 = q1.limit(5)
        q3 = q2.offset(10)
        q4 = q3.order_by("age", "desc")
        assert (q1._limit, q1._offset, q1.order_by_clause) == (None, None, [])
        assert q2._limit == 5 and q2._offset is None
        assert q3._offset == 10
        assert q4.order_by_clause == [
            OrderByEntry(column="age", direction="desc", path=())
        ]
        assert q3.order_by_clause == []

    def test_after_does_not_mutate(self):
        class ImmUserAfter(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        q1 = Query(ImmUserAfter).order_by("age").order_by("id")
        q2 = q1.after((18, 1)).limit(2)
        assert q1._after is None
        assert q2._after == (18, 1)
        assert q2 is not q1

    def test_before_does_not_mutate(self):
        class ImmUserBefore(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        q1 = Query(ImmUserBefore).order_by("age").order_by("id")
        q2 = q1.before((18, 1)).limit(2)
        assert q1._before is None
        assert q2._before == (18, 1)
        assert q2 is not q1

    def test_m2m_context_is_immutable_so_clones_share_it_safely(self):
        class ImmUser4(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        q1 = Query(ImmUser4)._m2m("jt", "src", "tgt", 1)
        q2 = q1.limit(3)
        assert q2._m2m_context == q1._m2m_context
        with pytest.raises(AttributeError):
            q2._m2m_context.join_table = "other"  # frozen: no aliasing hazard

    def test_relation_chaining_preserves_relation_type(self):
        class ImmUser5(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        r = Relation(ImmUser5).where(lambda u: u.age >= 1).limit(2)
        assert isinstance(r, Relation)


class TestTerminalsDoNotMutate:
    @pytest.mark.asyncio
    async def test_first_does_not_mutate_limit(self, tmp_path):
        class FirstImm(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str = ""

        db = tmp_path / "first_imm.db"
        await ferro.connect(f"sqlite:{db}?mode=rwc", auto_migrate=True)
        async with ferro.engines.session():
            await FirstImm(id=1, name="a").save()
            await FirstImm(id=2, name="b").save()
            q = FirstImm.select()
            got = await q.first()
            assert got is not None
            assert q._limit is None          # first() must not write _limit
            assert len(await q.all()) == 2   # q still returns everything

    @pytest.mark.asyncio
    async def test_exists_does_not_mutate(self, tmp_path):
        class ExistsImm(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        db = tmp_path / "exists_imm.db"
        await ferro.connect(f"sqlite:{db}?mode=rwc", auto_migrate=True)
        async with ferro.engines.session():
            await ExistsImm(id=1).save()
            q = ExistsImm.select()
            assert await q.exists() is True
            assert q._limit is None and q._offset is None
