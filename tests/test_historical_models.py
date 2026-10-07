"""Historical models (#530, ADR-0025): the union of a migration's two snapshots.

Migration ``0002_slugs`` renames ``author.name`` to ``full_name`` and adds a
required ``slug``. Between its expand and contract steps the live table holds
``id, full_name, bio, slug`` with ``slug`` still ``NULL`` on every row, and
that is exactly the historical ``Author`` a data step sees.
"""

from __future__ import annotations

import enum
import typing
from typing import Any

import pytest

import ferro
from ferro.migrations import historical
from ferro.migrations.historical import HistoricalModelError
from ferro.registry import REGISTRY

pytestmark = pytest.mark.usefixtures("clean_registry")


def col(name: str, logical_type: str = "string", **facts: Any) -> dict[str, Any]:
    return {
        "name": name,
        "logical_type": logical_type,
        "nullable": False,
        "primary_key": False,
        "autoincrement": False,
        "unique": False,
        "index": False,
        "default": None,
        "format": None,
        **facts,
    }


def pk(name: str = "id") -> dict[str, Any]:
    return col(name, "integer", primary_key=True, autoincrement=True)


def model(table: str, *columns: dict[str, Any], **facts: Any) -> dict[str, Any]:
    name = facts.pop("model_name", f"app.models.{table.title().replace('_', '')}")
    return {
        "model_name": name,
        "table_name": table,
        "columns": list(columns),
        "foreign_keys": [],
        "indexes": [],
        "uniques": [],
        "checks": [],
        **facts,
    }


def snapshot(*models: dict[str, Any]) -> dict[str, Any]:
    return {
        "ir_kind": "schema",
        "ir_version": 2,
        "payload": {"dialect_agnostic": True, "models": list(models)},
    }


PARENT = snapshot(
    model("author", pk(), col("name"), col("bio", nullable=True)),
    model("tag", pk(), col("label")),
    model(
        "author_tags",
        col("author_id", "integer"),
        col("tag_id", "integer"),
        model_name="author_tags",
    ),
)
OWN = snapshot(
    model("author", pk(), col("full_name", renamed_from="name"), col("slug")),
    model("tag", pk(), col("label")),
    model(
        "author_tags",
        col("author_id", "integer"),
        col("tag_id", "integer"),
        model_name="author_tags",
    ),
)


def fields(cls: type) -> dict[str, Any]:
    return {name: info.annotation for name, info in cls.model_fields.items()}


def test_the_union_holds_old_and_new_columns_a_rename_once_and_new_columns_nullable():
    models = historical.build(PARENT, OWN, rev="0002_slugs")

    author = models.Author
    assert fields(author) == {
        "id": int | None,
        "full_name": str,
        "bio": str | None,
        "slug": str | None,
    }
    assert author.__ferro_columns__["slug"].nullable is True
    assert author.__ferro_table__ == "author"
    assert author.__module__ == "ferro.migrations.historical.0002_slugs"
    assert models.names() == ["Author", "Tag", "author_tags"]


def test_a_join_table_is_reached_by_table_name():
    models = historical.build(PARENT, OWN, rev="0002_slugs")

    join = models.table("author_tags")
    assert fields(join) == {"author_id": int, "tag_id": int}
    with pytest.raises(LookupError, match="author, author_tags, tag"):
        models.table("post_tags")
    with pytest.raises(AttributeError, match="Author, Tag, author_tags"):
        models.Post  # noqa: B018 - the attribute access is what raises


def test_historical_classes_carry_columns_only_and_never_touch_todays_registry():
    parent = snapshot(model("author", pk()))
    own = snapshot(
        model("author", pk()),
        model(
            "post",
            pk(),
            col("author_id", "integer"),
            foreign_keys=[
                {
                    "column": "author_id",
                    "to_table": "author",
                    "to_column": "id",
                    "on_delete": "CASCADE",
                    "name": "fk_post_author_id_author",
                }
            ],
        ),
    )
    today = dict(REGISTRY.models())

    models = historical.build(parent, own, rev="0003_posts")

    post = models.Post
    assert fields(post) == {"id": int | None, "author_id": int}
    assert post.ferro_relations == {}
    assert post.model_config.get("use_attribute_docstrings") is False
    assert REGISTRY.models() == today


def test_an_any_column_keeps_its_db_type():
    own = snapshot(
        model(
            "event",
            pk(),
            col("payload", "unknown", db_type="jsonb", db_type_explicit=True),
            col("tags", "json", db_type="jsonb", db_type_explicit=True, nullable=True),
        )
    )

    models = historical.build_single(own, "0001_events")

    event = models.Event
    assert fields(event)["payload"] is typing.Any
    assert event.__ferro_columns__["payload"].db_type == "jsonb"
    assert event.__ferro_columns__["tags"].db_type == "jsonb"
    assert event.__ferro_columns__["tags"].logical_type == "json"


def test_an_enum_holds_the_labels_of_both_snapshots():
    status = {"enum_type_name": "status"}
    parent = snapshot(
        model("author", pk(), col("status", enum_values=["a", "b"], **status))
    )
    own = snapshot(
        model("author", pk(), col("status", enum_values=["b", "c"], **status))
    )

    models = historical.build(parent, own, rev="0002_status")

    enum_cls = fields(models.Author)["status"]
    assert issubclass(enum_cls, enum.Enum)
    assert [member.value for member in enum_cls] == ["a", "b", "c"]
    with pytest.raises(ValueError, match="enum type status holds the label 'zz'"):
        enum_cls("zz")


def test_a_column_that_stops_accepting_null_is_nullable_in_the_union():
    parent = snapshot(model("author", pk(), col("bio", nullable=True)))
    own = snapshot(model("author", pk(), col("bio")))

    models = historical.build(parent, own, rev="0002_require_bio")

    assert fields(models.Author)["bio"] == str | None


def test_a_same_named_column_declared_differently_is_refused_naming_both():
    parent = snapshot(model("author", pk(), col("age", "integer")))
    own = snapshot(model("author", pk(), col("age", "string", nullable=True)))

    with pytest.raises(HistoricalModelError) as refused:
        historical.build(parent, own, rev="0002_age")

    message = str(refused.value)
    assert "author.age" in message
    assert "integer, not null" in message
    assert "string, nullable" in message
    assert "0002_age" in message


def test_a_rename_hint_the_parent_cannot_honour_is_drop_plus_add():
    """The parent has no ``name``: the hint is dead, and the new column is a
    column only this migration adds (nullable)."""
    parent = snapshot(model("author", pk(), col("title")))
    own = snapshot(model("author", pk(), col("full_name", renamed_from="name")))

    models = historical.build(parent, own, rev="0002_dead_hint")

    assert fields(models.Author) == {
        "id": int | None,
        "title": str,
        "full_name": str | None,
    }


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_live_enum_label_neither_snapshot_declares_fails_hydration(
    db_url, db_backend
):
    await ferro.connect(db_url, name="ds_hist")
    async with ferro.transaction(using="ds_hist") as tx:
        if db_backend == "postgres":
            await tx.execute("CREATE TYPE ds_mood AS ENUM ('calm', 'busy', 'archived')")
            await tx.execute(
                "CREATE TABLE ds_person (id SERIAL PRIMARY KEY, mood ds_mood NOT NULL)"
            )
            await tx.execute("INSERT INTO ds_person (mood) VALUES ('archived')")
        else:
            await tx.execute(
                "CREATE TABLE ds_person (id INTEGER PRIMARY KEY, mood TEXT NOT NULL)"
            )
            await tx.execute("INSERT INTO ds_person (mood) VALUES ('archived')")
    mood = col("mood", enum_values=["calm", "busy"], enum_type_name="ds_mood")
    models = historical.build_single(
        snapshot(model("ds_person", pk(), mood, model_name="app.DsPerson")), "0004_mood"
    )

    with REGISTRY.swap(models):
        async with ferro.transaction(using="ds_hist"):
            with pytest.raises(Exception, match="ds_mood holds the label 'archived'"):
                await models.DsPerson.all()


def test_a_historical_json_column_is_a_dict_or_list_union_and_hydrates():
    own = snapshot(
        model(
            "event",
            pk(),
            col("payload", "unknown", db_type="jsonb", db_type_explicit=True),
            col("tags", "json", db_type="jsonb", db_type_explicit=True),
        )
    )

    event = historical.build_single(own, "0001_events").Event

    assert fields(event)["payload"] is typing.Any
    assert fields(event)["tags"] == dict[str, Any] | list[Any]
    row = event(id=1, payload={"a": 1}, tags=[1, 2])
    assert row.payload == {"a": 1}
    assert row.tags == [1, 2]
    assert event.__ferro_columns__["tags"].logical_type == "json"


def test_the_historical_marker_does_not_leak_to_user_models():
    own = snapshot(model("event", pk(), col("n", "integer")))
    historical.build_single(own, "0001_events")

    with pytest.raises(TypeError, match="incompatible"):

        class Event(ferro.Model):
            id: int | None = ferro.Field(default=None, primary_key=True)
            payload: Any = ferro.Field(db_type="jsonb")

    class Plain(ferro.Model):
        id: int | None = ferro.Field(default=None, primary_key=True)

    assert Plain.__dict__.get("__ferro_historical__") is False
