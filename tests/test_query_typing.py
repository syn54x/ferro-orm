"""Integration tests for typed query predicates.

Covers the three predicate styles accepted by ``Query.where`` and
``Relation.where`` — operator (``Model.field == value``), ``col()`` wrapper,
and lambda predicate (``lambda t: t.field == value``) — plus the AE5
mixed-style chain from the requirements doc.
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
from ferro.query import FieldProxy, Predicate, Query, QueryNode, QueryProxy, col

pytestmark = pytest.mark.sqlite_only


@pytest.fixture(autouse=True)
def _clear_state():
    """Reset model registry and engine between typing tests."""
    from ferro.state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    reset_engine()
    clear_registry()
    yield


# ---------------------------------------------------------------------------
# col() runtime behavior
# ---------------------------------------------------------------------------


class TestColWrapper:
    def test_col_returns_field_proxy_with_same_column(self):
        """col(FieldProxy) returns a typed query proxy for the same column."""

        class ColUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            archived: bool = False

        wrapped = col(ColUser.archived)  # type: ignore[arg-type]
        assert isinstance(wrapped, FieldProxy)
        assert wrapped.column == "archived"

    def test_col_eq_builds_query_node(self):
        """col(field) == value builds a QueryNode with the right shape."""

        class ColUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            archived: bool = False

        node = col(ColUser.archived) == False  # noqa: E712
        assert isinstance(node, QueryNode)
        assert node.column == "archived"
        assert node.operator == "=="
        assert node.value is False

    def test_col_rejects_literal_bool(self):
        """col(False) raises TypeError naming the bad type."""
        with pytest.raises(TypeError, match="bool"):
            col(False)  # type: ignore[arg-type]

    def test_col_rejects_string(self):
        """col('archived') raises TypeError naming the bad type."""
        with pytest.raises(TypeError, match="str"):
            col("archived")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_col_query_round_trips(self, db_url):
        """col() predicates execute against the real backend."""

        class ColUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            username: str
            archived: bool = False

        await ferro.connect(db_url, auto_migrate=True)
        await ColUser(id=1, username="alice", archived=False).save()
        await ColUser(id=2, username="bob", archived=True).save()

        active = await ColUser.where(col(ColUser.archived) == False).all()  # noqa: E712
        assert {u.username for u in active} == {"alice"}


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
            Query(LamUser).where(lambda t: True)  # type: ignore[arg-type, return-value]  # ty: ignore[no-matching-overload]

    def test_where_rejects_non_node_non_callable(self):
        """A bare value (not a QueryNode and not callable) is rejected."""

        class LamUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        with pytest.raises(TypeError, match="QueryNode or predicate callable"):
            Query(LamUser).where(123)  # type: ignore[arg-type]  # ty: ignore[no-matching-overload]

    def test_query_proxy_attribute_returns_field_proxy(self):
        """QueryProxy attribute access yields a FieldProxy at runtime."""
        proxy = QueryProxy()
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
        await LamUser(id=1, username="alice", archived=False).save()
        await LamUser(id=2, username="bob", archived=True).save()

        active = await LamUser.where(lambda t: t.archived == False).all()  # noqa: E712
        assert {u.username for u in active} == {"alice"}


# ---------------------------------------------------------------------------
# Operator path (regression — must keep working unchanged)
# ---------------------------------------------------------------------------


class TestOperatorPathUnchanged:
    pytestmark = pytest.mark.deprecated_operator_path

    @pytest.mark.asyncio
    async def test_operator_eq_still_works(self, db_url):
        """The original ``Model.field == value`` form is unchanged at runtime.

        This is the exact static-typing scenario that motivates ``col()`` and
        the lambda predicate API — ``OpUser.email == "a@b.com"`` resolves to
        ``bool`` statically, so ``ty`` rejects it. The runtime path is
        unaffected; the ``ty: ignore`` documents the trade-off rather than
        hiding it.
        """

        class OpUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            email: str

        await ferro.connect(db_url, auto_migrate=True)
        await OpUser(id=1, email="a@b.com").save()
        await OpUser(id=2, email="c@d.com").save()

        with pytest.deprecated_call(match="Operator predicate style.*v0\\.13\\.0"):
            rows = await OpUser.where(
                OpUser.email == "a@b.com"
            ).all()  # ty: ignore[no-matching-overload]
        assert len(rows) == 1
        assert rows[0].email == "a@b.com"


# ---------------------------------------------------------------------------
# AE5 — mixed chain
# ---------------------------------------------------------------------------


class TestCombinedStyles:
    pytestmark = pytest.mark.deprecated_operator_path

    @pytest.mark.asyncio
    async def test_mixed_chain_executes(self, db_url):
        """Operator + col() + lambda chained together filter correctly."""

        class MixUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            role: str = "user"
            archived: bool = False

        await ferro.connect(db_url, auto_migrate=True)
        await MixUser(id=1, role="admin", archived=False).save()
        await MixUser(id=1_001, role="admin", archived=True).save()
        await MixUser(id=2, role="user", archived=False).save()

        with pytest.deprecated_call(match="Operator predicate style.*v0\\.13\\.0"):
            rows = await (
                MixUser.where(MixUser.id == 1)  # ty: ignore[no-matching-overload]
                .where(col(MixUser.archived) == False)  # noqa: E712
                .where(lambda t: t.role == "admin")
                .all()
            )
        assert len(rows) == 1
        assert rows[0].id == 1


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

    # col() narrows back to FieldProxy[T]
    assert_type(col(_StaticUser.archived), FieldProxy[bool])

    # col(field) == value resolves to QueryNode (not bool)
    assert_type(col(_StaticUser.archived) == False, QueryNode)  # noqa: E712

    # Lambda predicates type-check as Predicate[Model]
    _pred: Predicate[_StaticUser] = lambda t: t.archived == False  # noqa: E712
    assert_type(_pred(QueryProxy[_StaticUser]()), QueryNode)
