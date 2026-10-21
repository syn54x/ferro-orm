"""The rendered statement: every `Query` terminal's `.sql()` (ADR-0057).

```python
query = RsAccount.where(lambda account: account.visits >= 18)
rendered = await query.all.sql()
rendered.sql      # 'SELECT ... WHERE "rsaccount"."visits" >= $1'
rendered.params   # (18,)
rendered.dialect  # 'postgres'
```

`.sql()` returns what the call would send without sending it: the same SQL
and parameters the statement log records for the call, built by the same
engine function, for the database the call would run on, with secrets shown
as `REDACTED`. The parity pin below holds that for every terminal on both
dialects: the rendered statement equals the `ferro.sql` record the executed
call writes.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import pytest
from pydantic import SecretStr

import ferro
from ferro import (
    REDACTED,
    BackRef,
    Model,
    Relation,
    RenderedSql,
    engines,
)
from ferro._core import identity_map_len
from ferro.base import FerroField, ForeignKey

pytestmark = pytest.mark.backend_matrix


class RsAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    email: Annotated[str, FerroField(unique=True)]
    token: SecretStr
    visits: int = 0
    notes: Relation[list["RsNote"]] = BackRef()


class RsNote(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
    account: Annotated[RsAccount | None, ForeignKey(related_name="notes")] = None


async def _seeded(db_url: str) -> RsAccount:
    """Connect, and save one account with one note."""
    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        ada = RsAccount(
            id=1, email="ada@example.com", token=SecretStr("sk_live_1"), visits=3
        )
        await ada.save()
        await RsNote(id=1, label="hello", account=ada).save()
    return ada


def _sent(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """The records of the statements the call itself sent: everything but the
    pass `connect` runs and the catalog probe of a table's first touch."""
    return [r for r in records if r.operation not in ("migrate", "catalog")]


def _sentinel_positions(params: tuple[object, ...]) -> list[int]:
    return [i for i, value in enumerate(params) if value is REDACTED]


# Each case: (name, the terminal to render and call, its arguments, whether a
# parameter is a secret). The terminal is read from a fresh query each time.
Terminal = Callable[[RsAccount], Any]
CASES: list[tuple[str, Terminal, tuple[Any, ...], dict[str, Any], bool]] = [
    (
        "all-secret-predicate",
        lambda ada: RsAccount.where(lambda account: account.token == "sk_live_1").all,
        (),
        {},
        True,
    ),
    (
        "all-include",
        lambda ada: (
            RsNote.select()
            .include(lambda note: note.account)
            .where(lambda note: note.label == "hello")
            .order_by(lambda note: note.id)
            .all
        ),
        (),
        {},
        False,
    ),
    (
        "first",
        lambda ada: (
            RsAccount.where(lambda account: account.visits >= 1)
            .order_by(lambda account: account.id)
            .first
        ),
        (),
        {},
        False,
    ),
    (
        "count-traversed-secret",
        lambda ada: RsNote.where(lambda note: note.account.token == "sk_live_1").count,
        (),
        {},
        True,
    ),
    (
        "exists",
        lambda ada: (
            RsAccount.where(lambda account: account.email == "ada@example.com").exists
        ),
        (),
        {},
        False,
    ),
    (
        "update-fields-secret",
        lambda ada: RsAccount.where(lambda account: account.id == 1).update,
        (),
        {"token": SecretStr("sk_live_2")},
        True,
    ),
    (
        "update-recipe",
        lambda ada: RsAccount.where(lambda account: account.id == 1).update,
        (lambda account: {"visits": account.visits + 1},),
        {},
        False,
    ),
    (
        "delete-secret",
        lambda ada: (
            RsAccount.where(lambda account: account.token == "sk_live_1").delete
        ),
        (),
        {},
        True,
    ),
    (
        "projected-all",
        lambda ada: (
            RsAccount.select(lambda account: account.email)
            .where(lambda account: account.visits >= 1)
            .all
        ),
        (),
        {},
        False,
    ),
    (
        "projected-count",
        lambda ada: (
            RsAccount.select(lambda account: account.email)
            .where(lambda account: account.token == "sk_live_1")
            .count
        ),
        (),
        {},
        True,
    ),
    (
        "all-after-plain-position-on-a-secret-key",
        lambda ada: (
            RsAccount.select()
            .order_by(lambda account: account.token)
            .order_by(lambda account: account.id)
            .after(("sk_live_CURSOR", 0))
            .all
        ),
        (),
        {},
        True,
    ),
    (
        "all-before-plain-position-on-a-secret-key",
        lambda ada: (
            RsAccount.select()
            .order_by(lambda account: account.token)
            .order_by(lambda account: account.id)
            .before(("sk_live_CURSOR", 0))
            .all
        ),
        (),
        {},
        True,
    ),
    (
        "first-after-plain-position-on-a-traversed-secret-key",
        lambda ada: (
            RsNote.select()
            .order_by(lambda note: note.account.token)
            .order_by(lambda note: note.id)
            .after(("sk_live_CURSOR", 0))
            .first
        ),
        (),
        {},
        True,
    ),
    ("relation-all", lambda ada: ada.notes.all, (), {}, False),
    ("relation-first", lambda ada: ada.notes.first, (), {}, False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "args", "kwargs", "secret"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
async def test_the_rendered_statement_is_the_logged_statement_of_the_call(
    db_url, db_backend, statement_records, terminal, args, kwargs, secret
):
    """The parity pin: `.sql()` equals the call's `ferro.sql` record in
    `sql`, `params` (the `REDACTED` sentinel included) and `dialect`."""
    ada = await _seeded(db_url)
    async with engines.session():
        rendered = await terminal(ada).sql(*args, **kwargs)
        statement_records.clear()
        await terminal(ada)(*args, **kwargs)

    (record,) = _sent(statement_records)
    assert isinstance(rendered, RenderedSql)
    assert rendered.sql == record.sql
    assert rendered.params == record.params
    assert _sentinel_positions(rendered.params) == _sentinel_positions(record.params)
    assert rendered.dialect == record.dialect == db_backend
    assert bool(_sentinel_positions(rendered.params)) is secret
    assert "sk_live" not in repr(rendered.params)


@pytest.mark.asyncio
async def test_a_rendered_statement_prints_as_its_sql(db_url, db_backend):
    await _seeded(db_url)
    async with engines.session():
        rendered = await RsAccount.where(lambda account: account.visits >= 18).all.sql()

    placeholder = "$1" if db_backend == "postgres" else "?"
    assert str(rendered) == rendered.sql
    assert rendered.sql.startswith("SELECT ")
    assert f'"rsaccount"."visits" >= {placeholder}' in rendered.sql
    assert rendered.params == (18,)
    assert rendered.dialect == db_backend


def test_a_rendered_statement_is_frozen():
    rendered = RenderedSql(sql="SELECT 1", params=(), dialect="sqlite")
    with pytest.raises(AttributeError):
        rendered.sql = "SELECT 2"  # type: ignore[misc]  # ty: ignore[invalid-assignment]


@pytest.mark.asyncio
async def test_update_sql_takes_both_forms_and_renders_them_differently(db_url):
    await _seeded(db_url)
    query = RsAccount.where(lambda account: account.id == 1)
    async with engines.session():
        fields = await query.update.sql(visits=19)
        recipe = await query.update.sql(lambda account: {"visits": account.visits + 1})

    assert fields.sql.startswith("UPDATE ")
    assert fields.params == (19, 1)
    assert recipe.sql.startswith("UPDATE ")
    assert recipe.params != fields.params


@pytest.mark.asyncio
async def test_first_sql_renders_the_limit_one_statement(db_url):
    await _seeded(db_url)
    async with engines.session():
        first = await RsAccount.where(lambda account: account.id == 1).first.sql()
        every = await RsAccount.where(lambda account: account.id == 1).all.sql()

    assert "LIMIT" in first.sql
    assert "LIMIT" not in every.sql


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["update", "delete"])
async def test_rendering_a_mutation_changes_no_row_and_sends_only_a_catalog_probe(
    db_url, db_backend, statement_records, verb
):
    await _seeded(db_url)
    statement_records.clear()
    async with engines.session() as session:
        query = RsAccount.where(lambda account: account.id == 1)
        if verb == "update":
            await query.update.sql(visits=99)
        else:
            await query.delete.sql()
        sent = list(statement_records)
        assert identity_map_len(session.session_id) == 0
        statement_records.clear()
        rows = await RsAccount.where(lambda account: account.id == 1).all()

    # Seeding already touched the table, so its catalog is cached.
    assert sent == []
    assert [(row.id, row.visits) for row in rows] == [(1, 3)]


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_rendering_on_a_tables_first_touch_sends_only_the_catalog_probe(
    db_url, statement_records
):
    """The casts in a rendered statement are the ones the call would use, so
    on a table's first touch rendering reads its catalog: one `catalog`
    record, and no record of the statement itself."""
    await ferro.connect(db_url, auto_migrate=True)
    statement_records.clear()
    async with engines.session():
        rendered = await RsAccount.where(lambda account: account.id == 1).delete.sql()
        first = [record.operation for record in statement_records]
        statement_records.clear()
        again = await RsAccount.where(lambda account: account.id == 1).delete.sql()

    assert first == ["catalog"]
    assert statement_records == []
    assert again == rendered


@pytest.mark.asyncio
async def test_rendering_a_fetch_puts_nothing_in_the_identity_map(db_url):
    await _seeded(db_url)
    async with engines.session() as session:
        await RsAccount.where(lambda account: account.id == 1).all.sql()
        await RsAccount.where(lambda account: account.id == 1).first.sql()
        assert identity_map_len(session.session_id) == 0


TERMINALS: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
    ("all", lambda: RsAccount.where(lambda account: account.id == 1).all.sql()),
    ("first", lambda: RsAccount.where(lambda account: account.id == 1).first.sql()),
    ("count", lambda: RsAccount.where(lambda account: account.id == 1).count.sql()),
    ("exists", lambda: RsAccount.where(lambda account: account.id == 1).exists.sql()),
    (
        "update",
        lambda: RsAccount.where(lambda account: account.id == 1).update.sql(visits=2),
    ),
    ("delete", lambda: RsAccount.where(lambda account: account.id == 1).delete.sql()),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "render", [t[1] for t in TERMINALS], ids=[t[0] for t in TERMINALS]
)
async def test_rendering_outside_a_session_raises_the_no_route_error(db_url, render):
    await _seeded(db_url)
    with pytest.raises(RuntimeError, match="No database route for this operation"):
        await render()


@pytest.mark.asyncio
async def test_update_sql_refuses_mixed_forms_like_update(db_url):
    await _seeded(db_url)
    query = RsAccount.where(lambda account: account.id == 1)
    async with engines.session():
        with pytest.raises(
            TypeError, match="either a recipe callable or keyword"
        ) as call:
            await query.update(lambda account: {"visits": 1}, visits=1)
        with pytest.raises(
            TypeError, match="either a recipe callable or keyword"
        ) as render:
            await query.update.sql(lambda account: {"visits": 1}, visits=1)
    assert str(call.value) == str(render.value)


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_using_and_session_route_the_rendering_to_their_database(
    db_url, tmp_path
):
    """`using=` and `session=` pick the database: the default is SQLite,
    `pg` is Postgres, and each rendering speaks its route's dialect."""
    await ferro.connect(f"sqlite:{tmp_path / 'app.db'}?mode=rwc", default=True)
    await ferro.create_tables()
    await ferro.connect(db_url, name="pg", auto_migrate=True)

    async with engines.session():
        local = await RsAccount.where(lambda account: account.id == 1).all.sql()
    async with engines.session("pg"):
        by_using = await (
            RsAccount.using("pg").where(lambda account: account.id == 1).all.sql()
        )
    async with engines.session("pg") as pg:
        async with engines.session():
            by_session = await RsAccount.where(
                lambda account: account.id == 1, session=pg
            ).count.sql()

    assert (local.dialect, local.params) == ("sqlite", (1,))
    assert "?" in local.sql
    assert (by_using.dialect, by_using.params) == ("postgres", (1,))
    assert "$1" in by_using.sql
    assert (by_session.dialect, by_session.params) == ("postgres", (1,))
