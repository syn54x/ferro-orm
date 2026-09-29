"""PROTOTYPE — throwaway. Not part of ferro. Delete freely.

Answers wayfinder ticket #462 (map #452):

    Can ferro build a usable model class from a stored SchemaIR snapshot —
    fields as they were, ferro's query and write machinery attached, none of
    the user's methods — and run ``where(...).all()`` and ``save()`` through
    it against a live connection?

Shape (a script, not the skill's HTML demo: the question is feasibility
against the Rust core, so the artifact is the run itself):

    1. define "today's" models, connect to a scratch SQLite file, seed rows
    2. take each model's SchemaIR envelope — this is what a migration's
       ``ir.json`` would hold — and serialize it to a JSON string
    3. forget the real classes entirely (registry snapshot + deregister + del)
    4. from the JSON alone, build historical classes through the ordinary
       metaclass path and push the resulting modelset to Rust
    5. query (lambda predicate, order, keyset paging), mutate, save, verify
       with raw SQL
    6. restore the real registry and prove the real classes still work

The one reusable piece is ``historical_model`` (IR envelope → class). Everything
else is harness. Run:

    uv run python src/ferro/migrations/prototype_historical_model.py
"""


import asyncio
import gc
import json
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

# --------------------------------------------------------------------------
# The reusable piece: IR envelope -> historical model class
# --------------------------------------------------------------------------

_SCALARS: dict[str, Any] = {
    "integer": int,
    "string": str,
    "number": float,
    "decimal": Decimal,
    "boolean": bool,
    "datetime": datetime,
    "uuid": UUID,
    "binary": bytes,
}


def _python_type(col: dict[str, Any]) -> Any:
    """Reverse ferro's logical_type derivation for one IR column."""
    from datetime import date, time

    lt = col["logical_type"]
    if col.get("enum_values") is not None:  # enums are logical "string" + labels
        # Labels are the storable values; the class name must round-trip to
        # the same ``enum_type_name`` (ferro lowercases the class name).
        name = col.get("enum_type_name") or f"{col['name']}_enum"
        return StrEnum(name, {v: v for v in col["enum_values"]})
    if lt == "date":
        return date
    if lt == "time":
        return time
    if lt == "json":
        return dict[str, Any] | list[Any]
    if lt in _SCALARS:
        return _SCALARS[lt]
    raise TypeError(f"cannot reverse logical_type {lt!r} for column {col['name']!r}")


def historical_model(envelope: dict[str, Any], *, module: str) -> type:
    """Build a Model subclass from one model's SchemaIR envelope.

    Goes through the ordinary metaclass so registration, column specs and the
    persisted envelope come from the same choke point every real class uses.
    The class carries nothing but columns: no user methods, no relations, no
    validators. ``module`` becomes part of ``__ferro_identity__`` so the class
    can never be confused with the codebase's class of the same name.
    """
    from pydantic import ConfigDict

    from ferro import Field, Model

    payload = envelope["payload"]["models"][0]
    class_name = payload["model_name"].rsplit(".", 1)[-1]
    annotations: dict[str, Any] = {}
    namespace: dict[str, Any] = {
        "__module__": module,
        "__qualname__": class_name,
        "__ferro_table__": payload["table_name"],
        "__annotations__": annotations,
        # ferro's base config reads attribute docstrings from *source*; a class
        # born from JSON has none, and pydantic raises. Finding for the real ctx.
        "model_config": ConfigDict(
            from_attributes=True, arbitrary_types_allowed=True, use_attribute_docstrings=False
        ),
    }
    for col in payload["columns"]:
        py = _python_type(col)
        kwargs: dict[str, Any] = {}
        if col["primary_key"]:
            kwargs["primary_key"] = True
        if col["unique"]:
            kwargs["unique"] = True
        if col["index"]:
            kwargs["index"] = True
        if col.get("db_type_explicit"):
            kwargs["db_type"] = col["db_type"]
        default = col.get("default")
        if col["nullable"]:
            annotations[col["name"]] = py | None
            namespace[col["name"]] = Field(default=default, **kwargs)
        else:
            annotations[col["name"]] = py
            if default is not None:
                namespace[col["name"]] = Field(default=default, **kwargs)
            elif kwargs:
                namespace[col["name"]] = Field(..., **kwargs)
    return type(class_name, (Model,), namespace)


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 70 - len(title)))


def columns_of(envelope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in envelope["payload"]["models"][0]["columns"]}


def diff_columns(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for name in sorted(set(columns_of(a)) | set(columns_of(b))):
        ca, cb = columns_of(a).get(name), columns_of(b).get(name)
        if ca is None or cb is None:
            out.append(f"{name}: present only in {'snapshot' if cb is None else 'historical'}")
            continue
        for key in sorted(set(ca) | set(cb)):
            if ca.get(key) != cb.get(key):
                out.append(f"{name}.{key}: snapshot={ca.get(key)!r} historical={cb.get(key)!r}")
    return out


async def main() -> None:
    import ferro
    from ferro import BackRef, Field, ForeignKey, Model, Relation
    from ferro import _push_registration_to_rust
    from ferro.raw import fetch_all
    from ferro.registry import REGISTRY

    # ---- 1. today's models -------------------------------------------------
    class Status(StrEnum):
        draft = "draft"
        live = "live"

    class Author(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        email: str = Field(unique=True)
        status: Status = Status.draft
        balance: Decimal = Decimal("0")
        joined_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
        prefs: dict[str, Any] = Field(default_factory=dict)
        nickname: str | None = None
        posts: Relation[list["Post"]] = BackRef()

        def shout(self) -> str:  # a user method the historical class must NOT carry
            return self.name.upper()

    class Post(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str = Field(index=True)
        author: Annotated[Author, ForeignKey(related_name="posts")]

    url = os.environ.get("FERRO_PROTO_URL")  # e.g. the local Postgres from the test matrix
    if url is None:
        db = os.path.join(tempfile.mkdtemp(prefix="ferro-proto-"), "PROTOTYPE-wipe-me.db")
        url = f"sqlite:{db}?mode=rwc"
    else:  # PROTOTYPE: wipe our two tables + enum type so auto_migrate starts clean
        from ferro.raw import execute as _exec

        await ferro.connect(url)
        async with ferro.engines.session():
            await _exec('DROP TABLE IF EXISTS "post"')
            await _exec('DROP TABLE IF EXISTS "author"')
            await _exec('DROP TYPE IF EXISTS "status"')
        ferro.reset_engine()
    print("backend:", url.split(":", 1)[0])
    await ferro.connect(url, auto_migrate=True)
    from contextlib import AsyncExitStack

    stack = AsyncExitStack()
    await stack.enter_async_context(ferro.engines.session())  # routes + identity map for the whole run

    a1 = await Author.create(name="Ada", email="ada@x", status=Status.live, balance=Decimal("1.50"))
    a2 = await Author.create(name="Bob", email="bob@x")
    a3 = await Author.create(name="Cy", email="cy@x", status=Status.live, prefs={"theme": "dark"})
    await Post.create(title="hello", author=a1)
    await Post.create(title="world", author=a3)

    section("1. seeded through today's classes")
    print(await fetch_all('SELECT id, name, status, balance, nickname FROM "author" ORDER BY id'))
    print(await fetch_all('SELECT id, title, author_id FROM "post" ORDER BY id'))

    # ---- 2. the snapshot a migration would store ---------------------------
    section("2. ir.json — the envelopes as a migration would store them")
    snapshot_json = json.dumps(
        {
            name: REGISTRY.envelope(cls.__ferro_identity__)
            for name, cls in (("Author", Author), ("Post", Post))
        },
        sort_keys=True,
    )
    print(f"{len(snapshot_json)} bytes; Author columns:")
    for c in json.loads(snapshot_json)["Author"]["payload"]["models"][0]["columns"]:
        print("   ", {k: v for k, v in c.items() if v not in (None, False)})
    print("Post foreign_keys:", json.loads(snapshot_json)["Post"]["payload"]["models"][0]["foreign_keys"])

    # ---- 3. forget the real classes ----------------------------------------
    section("3. forget today's classes (registry snapshot, deregister, del)")
    real_registry = REGISTRY.snapshot()
    for cls in (Post, Author):
        REGISTRY.deregister(cls.__ferro_identity__)
    real_author_identity = Author.__ferro_identity__
    del Author, Post, a1, a2, a3
    gc.collect()
    print("registered models now:", sorted(REGISTRY.models()))

    # ---- 4. rebuild from JSON alone ----------------------------------------
    section("4. historical classes from the JSON, through the metaclass")
    snap = json.loads(snapshot_json)
    module = "ferro.migrations.historical.rev0001"
    HAuthor = historical_model(snap["Author"], module=module)
    HPost = historical_model(snap["Post"], module=module)
    print("identity:", HAuthor.__ferro_identity__, "| table:", HAuthor.__ferro_table__)
    print("fields:  ", list(HAuthor.model_fields))
    print("has user method shout():", hasattr(HAuthor, "shout"))
    print("column diff vs snapshot (Author):", diff_columns(snap["Author"], REGISTRY.envelope(HAuthor.__ferro_identity__)) or "none")
    print("column diff vs snapshot (Post):  ", diff_columns(snap["Post"], REGISTRY.envelope(HPost.__ferro_identity__)) or "none")
    fk_hist = REGISTRY.envelope(HPost.__ferro_identity__)["payload"]["models"][0]["foreign_keys"]
    print("Post foreign_keys on the historical class:", fk_hist, "(expected: none — data steps don't emit DDL)")

    _push_registration_to_rust()
    print("pushed historical modelset to Rust; registered:", sorted(REGISTRY.models()))

    # ---- 5. query / page / mutate / save through the historical class ------
    section("5. query, keyset-page, mutate, save via the historical class")
    HStatus = HAuthor.model_fields["status"].annotation
    live = await HAuthor.where(lambda author: author.status == "live").order_by(lambda author: author.id).all()
    print("live authors:", [(a.id, a.name, a.status, a.balance, a.prefs) for a in live])
    print("status type on hydrated row:", type(live[0].status).__name__, "| balance type:", type(live[0].balance).__name__)

    page1 = await HAuthor.select().order_by(lambda author: author.id).limit(2).all()
    page2 = await HAuthor.select().order_by(lambda author: author.id).after(page1[-1]).limit(2).all()
    print("keyset page 1:", [a.id for a in page1], "page 2:", [a.id for a in page2])

    for a in await HAuthor.where(lambda author: author.nickname == None).all():  # noqa: E711
        a.nickname = a.name.lower()
        await a.save()
    print("after backfill:", await fetch_all('SELECT id, name, nickname FROM "author" ORDER BY id'))

    n = await HAuthor.where(lambda author: author.status == "draft").update(status="live")
    print("set-based update rows:", n, await fetch_all('SELECT id, status FROM "author" ORDER BY id'))

    posts = await HPost.select().order_by(lambda post: post.id).all()
    print("posts via historical class (shadow column, no relation):", [(p.id, p.title, p.author_id) for p in posts])
    print("HPost has 'author' relation attr:", hasattr(HPost, "author"))
    print("HStatus is a fresh enum:", HStatus, list(HStatus))

    # ---- 6. put the world back ---------------------------------------------
    section("6. restore today's registry and prove the real class still works")
    for cls in (HPost, HAuthor):
        REGISTRY.deregister(cls.__ferro_identity__)
    REGISTRY.restore(real_registry)
    _push_registration_to_rust()
    RealAuthor = REGISTRY.models()[real_author_identity]
    rows = await RealAuthor.select().order_by(lambda author: author.id).all()
    print("real class:", RealAuthor.__ferro_identity__, "| rows:", [(a.id, a.nickname, a.shout()) for a in rows])
    print("registered models now:", sorted(REGISTRY.models()))
    await stack.aclose()


if __name__ == "__main__":
    asyncio.run(main())
