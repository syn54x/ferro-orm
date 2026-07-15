"""Integration tests for typed query predicates.

Covers the lambda predicate style (``lambda t: t.field == value``) accepted
by ``Query.where`` and ``Relation.where`` — the only predicate style Ferro
supports since the v0.14.0 operator/``col()`` removal.
"""

from typing import TYPE_CHECKING, Annotated

import pytest

import ferro
from ferro import (
    BackRef,
    FerroField,
    ForeignKey,
    Model,
    Relation,
    clear_registry,
    reset_engine,
)
from ferro.query import FieldProxy, Predicate, Query, QueryNode, QueryProxy

pytestmark = pytest.mark.sqlite_only


@pytest.fixture(autouse=True)
def _clear_state():
    """Reset model registry and engine between typing tests."""
    from ferro.registry import REGISTRY

    REGISTRY.reset_for_test()
    reset_engine()
    clear_registry()
    yield


# ---------------------------------------------------------------------------
# Lambda predicate runtime behavior
# ---------------------------------------------------------------------------


class TestLambdaPredicates:
    def test_lambda_simple_appends_one_node(self):
        """A lambda predicate appends exactly one QueryNode."""

        class LamUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            archived: bool = False

        q: Query[LamUser] = Query(LamUser).where(lambda t: t.id == 1)
        assert len(q.where_clause) == 1
        assert q.where_clause[0].column == "id"
        assert q.where_clause[0].operator == "=="
        assert q.where_clause[0].value == 1

    def test_lambda_compound_predicate(self):
        """Compound predicates produce one is_compound=True node."""

        class LamUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            role: str = "user"
            active: bool = True

        q = Query(LamUser).where(
            lambda t: (t.role == "admin") & (t.active == True)  # noqa: E712
        )
        assert len(q.where_clause) == 1
        assert q.where_clause[0].is_compound is True
        assert q.where_clause[0].operator == "AND"

    def test_lambda_returning_non_query_node_raises(self):
        """Predicates that don't return a QueryNode are rejected."""

        class LamUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        with pytest.raises(TypeError, match="must return QueryNode"):
            Query(LamUser).where(lambda t: True)  # type: ignore[arg-type, return-value]  # ty: ignore[invalid-argument-type]

    def test_where_rejects_non_node_non_callable(self):
        """A non-callable value is rejected."""

        class LamUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        with pytest.raises(TypeError, match="predicate callable"):
            Query(LamUser).where(123)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def test_query_proxy_attribute_returns_field_proxy(self):
        """QueryProxy attribute access yields a validated FieldProxy at runtime."""

        class ProxyUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            archived: bool = False

        proxy = QueryProxy(ProxyUser)
        f = proxy.archived
        assert isinstance(f, FieldProxy)
        assert f.column == "archived"

    @pytest.mark.asyncio
    async def test_lambda_query_round_trips(self, db_url):
        """Lambda predicates execute against the real backend."""

        class LamUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            username: str
            archived: bool = False

        await ferro.connect(db_url, auto_migrate=True)
        async with ferro.engines.session():
            await LamUser(id=1, username="alice", archived=False).save()
            await LamUser(id=2, username="bob", archived=True).save()

            active = await LamUser.where(lambda t: t.archived == False).all()  # noqa: E712
            assert {u.username for u in active} == {"alice"}


# ---------------------------------------------------------------------------
# Relation.where parity
# ---------------------------------------------------------------------------


class TestRelationLambda:
    @pytest.mark.asyncio
    async def test_relation_where_accepts_lambda(self, db_url):
        """Relation.where accepts a lambda predicate (parity with Query)."""

        class RelAuthor(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str
            posts: Relation[list["RelPost"]] = BackRef()

        class RelPost(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            title: str
            published: bool = False
            author: Annotated[RelAuthor, ForeignKey(related_name="posts")]

        await ferro.connect(db_url, auto_migrate=True)
        async with ferro.engines.session():
            author = RelAuthor(id=1, name="taylor")
            await author.save()
            await RelPost(id=10, title="draft", published=False, author=author).save()
            await RelPost(id=11, title="live", published=True, author=author).save()

            published = await author.posts.where(lambda t: t.published == True).all()  # noqa: E712
            assert {p.title for p in published} == {"live"}


# ---------------------------------------------------------------------------
# Static-typing snippets for Pyright/`ty` to consume.
#
# These never execute at runtime; they exist so type checkers exercise the
# new generic types and confirm they resolve as advertised.
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    from typing import assert_type

    class _StaticUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        archived: bool = False
        email: str = ""

    # Lambda predicates type-check as Predicate[Model]
    _pred: Predicate[_StaticUser] = lambda t: t.archived == False  # noqa: E712
    assert_type(_pred(QueryProxy[_StaticUser](_StaticUser)), QueryNode)

    # FieldProxy comparisons resolve to QueryNode (the lambda mechanism)
    assert_type(FieldProxy("archived") == False, QueryNode)  # noqa: E712
