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
