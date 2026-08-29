"""FF-F F-2 exit-gate tests: misspelled columns fail at build time."""

from typing import Annotated

import pytest

from ferro import FerroField, ForeignKey, Model
from ferro.query import Query, QueryProxy
from ferro.query.wire import OrderByEntry


pytestmark = pytest.mark.sqlite_only


class TestWhereColumnValidation:
    def test_misspelled_column_raises_at_build_time(self):
        class ValUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str = ""

        with pytest.raises(AttributeError) as exc:
            ValUser.where(lambda u: u.nmae == "x")
        message = str(exc.value)
        assert "nmae" in message
        assert "name" in message        # did-you-mean + valid columns list
        assert "id" in message

    def test_valid_columns_pass(self):
        class ValUser2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str = ""

        q = ValUser2.where(lambda u: u.name == "x")
        assert q.where_clause[0].column == "name"

    def test_shadow_fk_column_is_valid(self):
        class ValAuthor(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        class ValPost(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            author: Annotated[ValAuthor, ForeignKey(related_name="val_posts")]

        q = Query(ValPost).where(lambda p: p.author_id == 1)
        assert q.where_clause[0].column == "author_id"

    def test_error_lists_shadow_fk_in_valid_columns(self):
        class ValAuthor2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        class ValPost2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            author: Annotated[ValAuthor2, ForeignKey(related_name="val_posts2")]

        with pytest.raises(AttributeError, match="author_id"):
            Query(ValPost2).where(lambda p: p.authr_id == 1)


class TestQueryProxyContract:
    def test_query_proxy_requires_model(self):
        with pytest.raises(TypeError):
            QueryProxy()  # model_cls is required — no unvalidated mode

    def test_query_proxy_validates_directly(self):
        class ValUser3(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        proxy = QueryProxy(ValUser3)
        assert proxy.id.column == "id"
        with pytest.raises(AttributeError, match="Valid columns"):
            _ = proxy.bogus

    def test_query_proxy_rejects_non_ferro_class(self):
        class NotAModel:
            pass

        from ferro.query.nodes import validate_query_column

        with pytest.raises(TypeError, match="not a registered Ferro model"):
            validate_query_column(NotAModel, "anything")


class TestOrderByValidation:
    def test_order_by_lambda_is_validated_and_extracts_column(self):
        class ObUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            created_at: str = ""

        q = ObUser.select().order_by(lambda u: u.created_at, "desc")
        assert q.order_by_clause == [
            OrderByEntry(column="created_at", direction="desc", path=())
        ]

    def test_order_by_string_is_validated(self):
        class ObUser2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        q = ObUser2.select().order_by("age")
        assert q.order_by_clause == [
            OrderByEntry(column="age", direction="asc", path=())
        ]

    def test_order_by_misspelled_string_raises(self):
        class ObUser3(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        with pytest.raises(AttributeError, match="age"):
            ObUser3.select().order_by("aeg")

    def test_order_by_misspelled_lambda_raises(self):
        class ObUser4(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        with pytest.raises(AttributeError, match="Valid columns"):
            ObUser4.select().order_by(lambda u: u.aeg)

    def test_order_by_lambda_must_return_field_proxy(self):
        class ObUser5(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        with pytest.raises(TypeError, match="FieldProxy"):
            ObUser5.select().order_by(lambda u: u.age >= 3)

    def test_order_by_rejects_bad_direction(self):
        class ObUser6(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        with pytest.raises(ValueError, match="asc"):
            ObUser6.select().order_by("id", "sideways")

    def test_order_by_nulls_first_and_last_accepted(self):
        class ObNullsUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            pinned_at: str | None = None

        q_first = ObNullsUser.select().order_by(
            lambda u: u.pinned_at, "desc", nulls="first"
        )
        assert q_first.order_by_clause == [
            OrderByEntry(
                column="pinned_at", direction="desc", path=(), nulls="first"
            )
        ]
        q_last = ObNullsUser.select().order_by("pinned_at", nulls="last")
        assert q_last.order_by_clause == [
            OrderByEntry(column="pinned_at", direction="asc", path=(), nulls="last")
        ]

    def test_order_by_nulls_case_insensitive(self):
        class ObNullsCase(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            pinned_at: str | None = None

        q = ObNullsCase.select().order_by("pinned_at", "DESC", nulls="LAST")
        assert q.order_by_clause[0].nulls == "last"
        q2 = ObNullsCase.select().order_by(lambda u: u.pinned_at, nulls="FIRST")
        assert q2.order_by_clause[0].nulls == "first"

    def test_order_by_nulls_rejects_junk(self):
        class ObNullsJunk(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            pinned_at: str | None = None

        with pytest.raises(ValueError, match=r"first.*last"):
            ObNullsJunk.select().order_by("pinned_at", nulls="sideways")
        with pytest.raises(ValueError, match=r"first.*last"):
            ObNullsJunk.select().order_by(
                lambda u: u.pinned_at, "desc", nulls="nulls last"
            )

    def test_order_by_nulls_is_keyword_only(self):
        class ObNullsKw(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        with pytest.raises(TypeError):
            ObNullsKw.select().order_by("id", "desc", "last")  # type: ignore[misc]

    def test_order_by_nulls_on_not_null_column(self):
        """nulls= is legal even when the column cannot store NULL."""

        class ObNotNull(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str = ""

        q = ObNotNull.select().order_by(lambda u: u.name, nulls="last")
        assert q.order_by_clause == [
            OrderByEntry(column="name", direction="asc", path=(), nulls="last")
        ]

    def test_order_by_omitted_nulls_matches_legacy_entry(self):
        class ObOmit(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        q = ObOmit.select().order_by(lambda u: u.age, "desc")
        assert q.order_by_clause == [
            OrderByEntry(column="age", direction="desc", path=())
        ]
        assert q.order_by_clause[0].nulls is None

    def test_order_by_nulls_with_relation_traversal(self):
        class ObAuthor(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str | None = None

        class ObPost(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            author: Annotated[ObAuthor, ForeignKey(related_name="ob_posts")]

        q = ObPost.select().order_by(lambda p: p.author.name, "desc", nulls="last")
        assert q.order_by_clause == [
            OrderByEntry(
                column="name", direction="desc", path=("author",), nulls="last"
            )
        ]

    def test_projected_order_by_nulls_output_alias_and_aggregate(self):
        class ObAggAccount(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            label: str | None = None

        class ObAggTxn(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            amount: int = 0
            account: Annotated[
                ObAggAccount | None, ForeignKey(related_name="ob_agg_txns")
            ] = None

        grouped = ObAggTxn.select(
            lambda t: {"acct": t.account_id, "total": t.amount.sum()}
        )
        by_alias = grouped.order_by("total", "desc", nulls="first")
        assert by_alias.order_by_clause == [
            OrderByEntry(column="total", direction="desc", path=(), nulls="first")
        ]
        by_agg = grouped.order_by(lambda t: t.amount.sum(), nulls="last")
        assert by_agg.order_by_clause == [
            OrderByEntry(column="total", direction="asc", path=(), nulls="last")
        ]
        by_traversal = ObAggTxn.select(
            lambda t: {"name": t.account.label, "total": t.amount.sum()}
        ).order_by(lambda t: t.account.label, "asc", nulls="first")
        assert by_traversal.order_by_clause == [
            OrderByEntry(
                column="label", direction="asc", path=("account",), nulls="first"
            )
        ]


class TestOperatorSurfaceRemoved:
    def test_class_attribute_is_not_a_field_proxy(self):
        class RmUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        with pytest.raises(AttributeError):
            RmUser.age  # normal Pydantic v2 class-attribute semantics restored

    def test_col_is_gone(self):
        with pytest.raises(ImportError):
            from ferro.query import col  # noqa: F401

    def test_where_rejects_raw_query_node(self):
        from ferro.query import QueryNode

        class RmUser2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        with pytest.raises(TypeError, match="predicate callable"):
            RmUser2.where(QueryNode("id", "==", 1))
