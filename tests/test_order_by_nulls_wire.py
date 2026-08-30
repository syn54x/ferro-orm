"""Build-time wire shape for ``order_by(..., nulls=...)`` (#362, #392).

Stops at ``compile_query`` — no SQL execution, no row-order assertions.
"""

import json
from typing import Annotated

import pytest

from ferro import FerroField, ForeignKey, Model
from ferro.query.wire import compile_query

pytestmark = pytest.mark.sqlite_only


def test_compile_query_emits_nulls_on_every_term():
    class WireCard(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        pinned_at: str | None = None
        updated_at: str = ""

    query = (
        WireCard.select()
        .order_by(lambda c: c.pinned_at, "desc", nulls="last")
        .order_by(lambda c: c.updated_at, "desc")
    )
    envelope = json.loads(compile_query(query, "fetch").wire_json)
    assert envelope["ir_version"] == 14
    order_by = envelope["payload"]["order_by"]
    assert order_by == [
        {
            "column": "pinned_at",
            "direction": "desc",
            "path": [],
            "nulls": "last",
        },
        {
            "column": "updated_at",
            "direction": "desc",
            "path": [],
            "nulls": "last",
        },
    ]


def test_compile_query_nulls_first_on_projected_query():
    class WireItem(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        amount: int = 0
        note: str | None = None

    query = WireItem.select(
        lambda t: {"note": t.note, "total": t.amount.sum()}
    ).order_by("total", "desc", nulls="first")
    envelope = json.loads(compile_query(query, "fetch").wire_json)
    assert envelope["ir_version"] == 14
    assert envelope["payload"]["order_by"] == [
        {
            "column": "total",
            "direction": "desc",
            "path": [],
            "nulls": "first",
        }
    ]


def test_compile_query_nulls_with_traversal_path():
    class WireAuthor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str | None = None

    class WirePost(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        author: Annotated[WireAuthor, ForeignKey(related_name="wire_posts")]

    query = WirePost.select().order_by(
        lambda p: p.author.name, "asc", nulls="first"
    )
    payload = compile_query(query, "fetch").payload.to_ir_dict()
    assert payload["order_by"] == [
        {
            "column": "name",
            "direction": "asc",
            "path": ["author"],
            "nulls": "first",
        }
    ]


def test_compile_query_native_nulls_on_wire():
    class WireNative(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        age: int = 0

    envelope = json.loads(
        compile_query(
            WireNative.select().order_by("age", "desc", nulls="native"),
            "fetch",
        ).wire_json
    )
    assert envelope["ir_version"] == 14
    entry = envelope["payload"]["order_by"][0]
    assert entry == {
        "column": "age",
        "direction": "desc",
        "path": [],
        "nulls": "native",
    }


def test_compile_query_omitted_nulls_emits_last_at_ir_version_14():
    class WirePlain(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        age: int = 0

    envelope = json.loads(
        compile_query(WirePlain.select().order_by("age"), "fetch").wire_json
    )
    assert envelope["ir_version"] == 14
    entry = envelope["payload"]["order_by"][0]
    assert entry == {
        "column": "age",
        "direction": "asc",
        "path": [],
        "nulls": "last",
    }
