"""FF-F F-2 exit-gate tests: misspelled columns fail at build time."""

from typing import Annotated

import pytest

from ferro import FerroField, ForeignKey, Model
from ferro.query import Query, QueryProxy


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
            {"column": "created_at", "direction": "desc", "path": []}
        ]

    def test_order_by_string_is_validated(self):
        class ObUser2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        q = ObUser2.select().order_by("age")
        assert q.order_by_clause == [{"column": "age", "direction": "asc", "path": []}]

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
