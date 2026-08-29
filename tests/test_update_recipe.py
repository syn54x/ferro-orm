"""Build-time doors for ``update(lambda t: {...})`` (#377 / #378).

Stops at compile / call time — no SQL execution. Backend persistence lives
in ``test_bulk_update.py``.
"""

from datetime import date, datetime, time
from typing import Annotated

import pytest

from ferro import BackRef, Field, FerroField, ForeignKey, Model, Relation, now
from ferro.query.nodes import QueryProxy, ValueExpr
from ferro.relations import resolve_relationships


@pytest.fixture()
def models(clean_registry: None) -> dict[str, type]:
    class Account(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str = ""
        users: Relation[list["User"]] = BackRef()

    class User(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: str = ""
        active: bool = True
        score: int = 0
        bonus: int = 0
        name: str = ""
        account: Annotated[Account | None, ForeignKey(related_name="users")] = None
        updated_at: datetime | None = None
        born: date | None = None
        opens_at: time | None = None
        turns: dict = Field(default_factory=dict)
        items: list = Field(default_factory=list)
        recipe: str = ""

    resolve_relationships()
    return {"User": User, "Account": Account}


@pytest.mark.asyncio
async def test_field_proxy_in_kwargs_names_recipe_form(
    models: dict[str, type],
) -> None:
    User = models["User"]
    proxy = QueryProxy(User).email
    with pytest.raises(TypeError, match=r"update\(lambda .*: \{\"col\":"):
        await User.where(lambda user: user.active == True).update(email=proxy)  # noqa: E712


@pytest.mark.asyncio
async def test_value_expr_in_kwargs_names_recipe_form(
    models: dict[str, type],
) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"update\(lambda .*: \{\"col\":"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            email=ValueExpr()
        )


@pytest.mark.asyncio
async def test_recipe_plus_kwargs_is_error(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"recipe|keyword"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"email": "x@ferro.dev"},
            bonus=1,
        )


@pytest.mark.asyncio
async def test_empty_recipe_is_error(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(ValueError, match=r"empty|at least one"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {}
        )


@pytest.mark.asyncio
async def test_non_dict_recipe_is_error(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"dict"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: ["email"]  # type: ignore[arg-type, return-value]
        )


@pytest.mark.asyncio
async def test_family_mismatch_on_copy_names_columns_and_families(
    models: dict[str, type],
) -> None:
    User = models["User"]
    with pytest.raises(
        TypeError,
        match=r"email.*string.*bonus.*integer|bonus.*integer.*email.*string",
    ):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"bonus": user.email}
        )


@pytest.mark.asyncio
async def test_traversed_source_is_error(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"travers"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"name": user.account.name}
        )


@pytest.mark.asyncio
async def test_relation_target_is_error(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"relation"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"account": user.score}
        )


@pytest.mark.asyncio
async def test_relation_source_is_error(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"relation"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"bonus": user.account}
        )


@pytest.mark.asyncio
async def test_unknown_recipe_key_uses_did_you_mean(
    models: dict[str, type],
) -> None:
    User = models["User"]
    with pytest.raises(AttributeError, match=r"Did you mean"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"emial": "x@ferro.dev"}
        )


def test_value_expr_in_select_names_followup(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"#327|#309"):
        User.select(lambda user: ValueExpr())


def test_value_expr_in_where_names_followup(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"#327|#309"):
        User.where(lambda user: ValueExpr())  # type: ignore[arg-type, return-value]


def test_value_expr_in_order_by_names_followup(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"#327|#309"):
        User.select().order_by(lambda user: ValueExpr())  # type: ignore[arg-type, return-value]


def test_select_field_proxy_still_projects(models: dict[str, type]) -> None:
    User = models["User"]
    query = User.select(lambda user: user.score)
    assert query._projection[0].column == "score"


def test_projected_query_recipe_stays_hard_reject(models: dict[str, type]) -> None:
    User = models["User"]
    projected = User.select(lambda user: (user.id,))
    with pytest.raises(
        ValueError, match=r"update\(\) is not supported on a projected query"
    ):
        projected.update(lambda user: {"email": "x@ferro.dev"})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_plus_on_non_numeric_column_names_family(
    models: dict[str, type],
) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"email.*string.*numeric"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"name": user.email + 1}
        )


@pytest.mark.asyncio
async def test_minus_on_non_numeric_column_names_family(
    models: dict[str, type],
) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"email.*string.*numeric"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"score": user.email - 1}
        )


@pytest.mark.asyncio
async def test_now_on_date_column_names_family(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"now.*born.*date"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"born": now}
        )


@pytest.mark.asyncio
async def test_now_on_time_column_names_family(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"now.*opens_at.*time"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"opens_at": now}
        )


@pytest.mark.asyncio
async def test_bare_value_expr_names_concat_followup(
    models: dict[str, type],
) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"\.concat\(\)"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"email": ValueExpr()}
        )


@pytest.mark.asyncio
async def test_kwargs_can_set_column_named_recipe(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(RuntimeError, match=r"No database route"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            recipe="soup"
        )


@pytest.mark.asyncio
async def test_merge_compile_uses_session_connection(
    models: dict[str, type], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ferro.query.builder import Query
    from ferro.query.wire import compile_query

    def backend(using: str | None = None) -> str:
        return "postgres" if using == "pg" else "sqlite"

    monkeypatch.setattr("ferro._core.connection_backend", backend)

    User = models["User"]
    recipe = {"turns": QueryProxy(User).turns.merge({"k": 1})}

    class _PgSession:
        session_id = "s"
        connection_name = "pg"

    compile_query(Query(User, session=_PgSession()), "update", recipe=recipe)

    class _SqliteSession:
        session_id = "s"
        connection_name = "app"

    with pytest.raises(TypeError, match=r"Postgres"):
        compile_query(Query(User, session=_SqliteSession()), "update", recipe=recipe)


@pytest.mark.asyncio
async def test_merge_on_sqlite_names_postgres(models: dict[str, type]) -> None:
    from ferro import connect, engines

    User = models["User"]
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        with pytest.raises(TypeError, match=r"Postgres"):
            await User.where(lambda user: user.active == True).update(  # noqa: E712
                lambda user: {"turns": user.turns.merge({"k": 1})}
            )


@pytest.mark.asyncio
async def test_merge_on_list_column_names_concat(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"\.concat\(\)"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"items": user.items.merge({"k": 1})}
        )


@pytest.mark.asyncio
async def test_merge_list_patch_names_concat(models: dict[str, type]) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"\.concat\(\)"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"turns": user.turns.merge(["not", "an", "object"])}  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_merge_on_non_json_column_is_build_time_error(
    models: dict[str, type],
) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"json object|object"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"email": user.email.merge({"k": 1})}
        )


@pytest.mark.asyncio
async def test_merge_target_non_object_is_build_time_error(
    models: dict[str, type],
) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"json object|object"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            lambda user: {"score": user.turns.merge({"k": 1})}
        )


@pytest.mark.asyncio
async def test_now_in_kwargs_still_names_recipe_form(
    models: dict[str, type],
) -> None:
    User = models["User"]
    with pytest.raises(TypeError, match=r"update\(lambda .*: \{\"col\":"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            updated_at=now
        )


@pytest.mark.asyncio
async def test_merge_in_kwargs_still_names_recipe_form(
    models: dict[str, type],
) -> None:
    User = models["User"]
    proxy = QueryProxy(User).turns
    with pytest.raises(TypeError, match=r"update\(lambda .*: \{\"col\":"):
        await User.where(lambda user: user.active == True).update(  # noqa: E712
            turns=proxy.merge({"k": 1})
        )
