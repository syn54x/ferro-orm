# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""The statement log on migration DDL (#764, ADR-0057).

```python
logging.getLogger("ferro.sql").setLevel(logging.DEBUG)
report = await ferro.migrate()
# ferro.sql DEBUG CREATE TABLE "post" (...) params=() 1.2ms
# ferro.sql DEBUG ALTER TABLE "author" ADD COLUMN "bio" varchar params=() 0.8ms
```

Every statement the reconciliation pass or a run sends is one record on
``ferro.sql`` as it finishes, carrying ``sql``, ``params``, ``dialect``,
``duration_ms``, ``operation``, ``subject``, ``connection``, ``rows`` and
``error``. The logger is the only switch: with ``ferro.sql`` off, nothing is
collected and the emitter is never called.
"""

import logging
from typing import Annotated

import pytest
from pydantic import SecretBytes, SecretStr

import ferro
import ferro._sql_log
from ferro import (
    BackRef,
    ManyToMany,
    Model,
    Relation,
    engines,
    raw,
    transaction,
)
from ferro._sql_log import REDACTED
from ferro.base import FerroField, ForeignKey
from ferro.exceptions import IntegrityError
from ferro.migrations.testing import harness
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    pkg,
    project,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    migrations,
    new,
    sql_step,
)

pytestmark = pytest.mark.backend_matrix

RECORD_ATTRIBUTES = (
    "sql",
    "params",
    "dialect",
    "duration_ms",
    "operation",
    "subject",
    "connection",
    "rows",
    "error",
)


# -- the reconciliation pass ----------------------------------------------------------


@pytest.fixture
def _fresh(clean_registry):
    yield
    ferro.reset_engine()


def _declare_author(*, bio: bool) -> None:
    if bio:

        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str
            bio: str | None = None

    else:

        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str


def _rewind() -> None:
    from ferro.registry import REGISTRY

    ferro.reset_engine()
    ferro.clear_registry()
    REGISTRY.reset_for_test()


def _pass_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """The records of statements the pass reports: those with a subject."""
    return [record for record in records if record.subject is not None]


def _assert_matches_report(records, report, backend: str) -> None:
    logged = _pass_records(records)
    assert [(r.subject, r.sql) for r in logged] == [
        (s.subject, s.sql) for s in report.statements
    ]
    for record in logged:
        assert record.name == "ferro.sql"
        assert record.levelno == logging.DEBUG
        assert record.operation == "migrate"
        assert record.params == ()
        assert record.dialect == backend
        assert record.connection == "default"
        assert isinstance(record.duration_ms, float) and record.duration_ms >= 0
        assert record.error is None
        assert record.getMessage() == (
            f"{record.sql} params=() {record.duration_ms:.1f}ms"
        )


@pytest.mark.usefixtures("_fresh")
@pytest.mark.asyncio
async def test_create_tables_logs_one_record_per_reported_statement(
    db_url, db_backend, statement_records
):
    _declare_author(bio=False)
    await ferro.connect(db_url)

    report = await ferro.create_tables()

    assert [s.subject for s in report.statements if s.role == "schema"] == ["author"]
    _assert_matches_report(statement_records, report, db_backend)
    for record in statement_records:
        assert {a: getattr(record, a) for a in RECORD_ATTRIBUTES}.keys() == set(
            RECORD_ATTRIBUTES
        )


@pytest.mark.usefixtures("_fresh")
@pytest.mark.asyncio
async def test_migrate_logs_the_alter_it_ran(db_url, db_backend, statement_records):
    _declare_author(bio=False)
    await ferro.connect(db_url)
    await ferro.create_tables()
    _rewind()
    _declare_author(bio=True)
    await ferro.connect(db_url)
    statement_records.clear()

    report = await ferro.migrate()

    _assert_matches_report(statement_records, report, db_backend)
    assert ("author", 'ALTER TABLE "author" ADD COLUMN "bio" varchar') in [
        (r.subject, r.sql) for r in statement_records
    ]


@pytest.mark.usefixtures("_fresh")
@pytest.mark.asyncio
async def test_with_the_log_off_the_emitter_is_never_called(db_url, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ferro._sql_log, "emit", calls.append)
    logger = logging.getLogger("ferro.sql")
    level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        _declare_author(bio=False)
        await ferro.connect(db_url)
        await ferro.create_tables()
        await ferro.migrate()
    finally:
        logger.setLevel(level)

    assert calls == []


@pytest.mark.usefixtures("_fresh")
@pytest.mark.asyncio
async def test_with_the_log_on_the_emitter_is_the_one_ferro_calls(
    db_url, monkeypatch, statement_records
):
    calls: list[dict] = []
    emit = ferro._sql_log.emit

    def spy(record: dict) -> None:
        calls.append(record)
        emit(record)

    monkeypatch.setattr(ferro._sql_log, "emit", spy)
    _declare_author(bio=False)
    await ferro.connect(db_url)
    await ferro.create_tables()

    assert calls and len(calls) == len(statement_records)


@pytest.mark.usefixtures("_fresh")
@pytest.mark.asyncio
async def test_caplog_captures_statement_records_without_setup(db_url, caplog):
    caplog.set_level(logging.DEBUG)
    _declare_author(bio=False)
    await ferro.connect(db_url)

    report = await ferro.create_tables()

    sql = [r for r in caplog.records if r.name == "ferro.sql"]
    assert [r.sql for r in sql if r.subject is not None] == [
        s.sql for s in report.statements
    ]
    assert any(r.name == "ferro" for r in caplog.records)


def test_the_ferro_logger_has_only_a_null_handler_and_propagates():
    logger = logging.getLogger("ferro")
    assert [type(h) for h in logger.handlers] == [logging.NullHandler]
    assert logger.propagate is True


def test_redacted_reads_as_three_stars():
    assert repr(ferro._sql_log.REDACTED) == "***"
    assert str(ferro._sql_log.REDACTED) == "***"


def test_emit_substitutes_redacted_for_masked_parameters(statement_records):
    ferro._sql_log.emit(
        {
            "sql": 'SELECT 1 WHERE "token" = ?',
            "params": (None, 7),
            "mask": (True, False),
            "dialect": "sqlite",
            "duration_ms": 0.25,
            "operation": "query",
            "subject": None,
            "connection": "default",
            "rows": None,
            "error": None,
        }
    )

    (record,) = statement_records
    assert record.params == (ferro._sql_log.REDACTED, 7)
    assert record.getMessage() == 'SELECT 1 WHERE "token" = ? params=(***, 7) 0.2ms'


# -- a run ----------------------------------------------------------------------------

AUTHOR = """
class Author(Model):
    __ferro_table__ = "slog_author"
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
"""

LOGS_A_LINE = """\
from ferro.migrations import atomic, nothing_to_reverse


@atomic
async def up(ctx):
    ctx.log.info("backfilled 3 rows")


@nothing_to_reverse("nothing was written")
def down(ctx): ...
"""


@pytest.fixture
def chain(project, pkg, db, clean_registry):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    return project


async def _connected(db) -> None:
    await ferro.connect(db.url)


@pytest.mark.usefixtures("isolated_imports")
@pytest.mark.asyncio
async def test_a_run_logs_each_step_statement_under_its_step_file(
    chain, db, statement_records
):
    sql_step(chain, "seed", "INSERT INTO \"slog_author\" (name) VALUES ('Ada');\n")
    await _connected(db)
    try:
        h = harness()
        await h.apply("0001")
        await h.apply("0002")
    finally:
        ferro.reset_engine()

    (ddl_file,) = (migrations(chain) / "0001_create_author").glob(
        f"*.up.{db.backend}.sql"
    )
    ddl = [
        r
        for r in statement_records
        if r.subject == f"0001_create_author/{ddl_file.name}"
        and "lock_timeout" not in r.sql
    ]
    assert ddl and all(r.sql in ddl_file.read_text() for r in ddl)
    seed = [
        r.sql
        for r in statement_records
        if r.subject == "0002_seed/01_seed.up.sql" and "lock_timeout" not in r.sql
    ]
    assert seed == ["INSERT INTO \"slog_author\" (name) VALUES ('Ada')"]
    for record in ddl:
        assert record.operation == "migrate"
        assert record.connection == "default"
        assert record.dialect == db.backend
        assert record.params == ()
        assert record.error is None


@pytest.mark.usefixtures("isolated_imports")
@pytest.mark.asyncio
async def test_a_statement_the_database_rejects_is_logged_with_its_error(
    chain, db, statement_records
):
    sql_step(chain, "broken", 'INSERT INTO "nowhere" VALUES (1);\n')
    await _connected(db)
    try:
        h = harness()
        await h.apply("0001")
        with pytest.raises(Exception) as raised:
            await h.apply("0002")
    finally:
        ferro.reset_engine()

    (failed,) = [
        r
        for r in statement_records
        if r.subject == "0002_broken/01_broken.up.sql" and r.error is not None
    ]
    assert failed.sql == 'INSERT INTO "nowhere" VALUES (1)'
    assert "nowhere" in failed.error
    assert failed.getMessage().endswith(f" failed: {failed.error}")
    assert "nowhere" in str(raised.value)


@pytest.mark.usefixtures("isolated_imports")
def test_ferro_migrate_up_prints_a_data_steps_log_line(chain, pkg, db, capsys):
    write_models(chain, pkg, AUTHOR + "    bio: str | None = None\n")
    new("add_bio", "--data-step", "Author")
    (step,) = (migrations(chain) / "0002_add_bio").glob("*.py")
    step.write_text(LOGS_A_LINE)
    capsys.readouterr()

    from tests.test_migrate_new import run

    assert run("migrate", "up", "--url", db.url) == 0

    assert "backfilled 3 rows" in capsys.readouterr().err
    logger = logging.getLogger("ferro")
    assert [type(h) for h in logger.handlers] == [logging.NullHandler]


# -- operations -----------------------------------------------------------------------
#
# Every statement an operation sends is one record, named by its operation, and
# a secret value never reaches a record.


class SlogAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    email: Annotated[str, FerroField(unique=True)]
    token: SecretStr
    key: SecretBytes | None = None
    notes: Relation[list["SlogNote"]] = BackRef()
    tags: Relation[list["SlogTag"]] = ManyToMany(related_name="accounts")


class SlogNote(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
    account: Annotated[SlogAccount | None, ForeignKey(related_name="notes")] = None


class SlogTag(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    accounts: Relation[list["SlogAccount"]] = BackRef()


class SlogItem(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
    n: int


TENANT_KEY = "myapp.tenant_id"


def _ph(backend: str, n: int = 1) -> str:
    return f"${n}" if backend == "postgres" else "?"


def _ada() -> SlogAccount:
    return SlogAccount(
        id=1,
        email="ada@example.com",
        token=SecretStr("sk_live_1"),
        key=SecretBytes(b"k\x00\xff"),
    )


def _sent(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """The records of statements the operations themselves sent: everything
    but the pass `connect` runs and the catalog probe, which runs only on a
    table's first touch."""
    return [r for r in records if r.operation not in ("migrate", "catalog")]


def _assert_hidden(records: list[logging.LogRecord], *secrets: object) -> None:
    for record in records:
        message = record.getMessage()
        for secret in secrets:
            assert secret not in record.params
            if isinstance(secret, str):
                assert secret not in message
            else:
                assert repr(secret) not in message


def _assert_shared(record: logging.LogRecord, backend: str) -> None:
    assert record.connection == "default"
    assert record.dialect == backend
    assert record.error is None


async def _connect(db_url: str) -> None:
    await ferro.connect(db_url, auto_migrate=True)


@pytest.mark.asyncio
async def test_a_fetch_is_one_fetch_record(db_url, db_backend, statement_records):
    await _connect(db_url)
    async with engines.session():
        ada = _ada()
        await ada.save()
        await SlogNote(id=1, label="hello", account=ada).save()
        statement_records.clear()

        notes = await (
            SlogNote.select()
            .include(lambda note: note.account)
            .where(lambda note: note.label == "hello")
            .order_by(lambda note: note.id)
            .all()
        )

    assert [note.id for note in notes] == [1]
    (record,) = _sent(statement_records)
    assert record.operation == "fetch"
    assert record.sql.startswith("SELECT")
    assert record.rows is None
    _assert_shared(record, db_backend)


@pytest.mark.asyncio
async def test_count_and_exists_name_their_own_records(
    db_url, db_backend, statement_records
):
    await _connect(db_url)
    async with engines.session():
        await _ada().save()
        statement_records.clear()

        assert await SlogAccount.where(lambda account: account.id == 1).count() == 1
        assert await SlogAccount.where(lambda account: account.id == 1).exists()

    count, exists = _sent(statement_records)
    assert (count.operation, exists.operation) == ("count", "exists")
    for record in (count, exists):
        assert "COUNT" in record.sql.upper()
        assert record.rows is None
        _assert_shared(record, db_backend)


@pytest.mark.asyncio
async def test_save_of_a_new_and_a_fetched_instance_is_save(
    db_url, db_backend, statement_records
):
    await _connect(db_url)
    async with engines.session():
        await _ada().save()
        fetched = await SlogAccount.get(1)
        fetched.email = "ada@lovelace.dev"
        await fetched.save()

    insert, get, update = _sent(statement_records)
    assert (insert.operation, get.operation, update.operation) == (
        "save",
        "fetch",
        "save",
    )
    assert insert.sql.startswith("INSERT INTO")
    assert update.sql.startswith("UPDATE")
    assert (insert.rows, update.rows) == (1, 1)
    for record in (insert, get, update):
        _assert_shared(record, db_backend)


@pytest.mark.asyncio
async def test_bulk_create_leaves_one_record_per_chunk(
    db_url, db_backend, statement_records
):
    await _connect(db_url)
    rows = 33_000  # two binds a row: past one statement's bind limit on both
    async with engines.session():
        inserted = await SlogItem.bulk_create(
            [SlogItem(label="x", n=i) for i in range(rows)]
        )

    assert inserted == rows
    inserts = [r for r in _sent(statement_records) if r.sql.startswith("INSERT")]
    assert len(inserts) >= 2
    assert {r.operation for r in _sent(statement_records)} == {"bulk_create"}
    assert sum(r.rows for r in inserts) == rows


@pytest.mark.asyncio
async def test_model_delete_is_delete(db_url, db_backend, statement_records):
    await _connect(db_url)
    async with engines.session():
        tag = SlogTag(id=1, name="a")
        await tag.save()
        statement_records.clear()

        await tag.delete()

    (record,) = _sent(statement_records)
    assert (record.operation, record.rows) == ("delete", 1)
    assert record.sql.startswith("DELETE")
    _assert_shared(record, db_backend)


@pytest.mark.asyncio
async def test_query_update_and_delete_name_their_records(
    db_url, db_backend, statement_records
):
    await _connect(db_url)
    async with engines.session():
        await SlogTag(id=1, name="a").save()
        await SlogTag(id=2, name="b").save()
        statement_records.clear()

        await SlogTag.where(lambda tag: tag.id <= 2).update(name="z")
        await SlogTag.where(lambda tag: tag.id == 1).delete()

    update, delete = _sent(statement_records)
    assert (update.operation, update.rows) == ("update", 2)
    assert (delete.operation, delete.rows) == ("delete", 1)
    for record in (update, delete):
        _assert_shared(record, db_backend)


@pytest.mark.asyncio
async def test_many_to_many_add_remove_and_clear(db_url, db_backend, statement_records):
    await _connect(db_url)
    async with engines.session():
        account = _ada()
        await account.save()
        red = SlogTag(id=1, name="red")
        blue = SlogTag(id=2, name="blue")
        await red.save()
        await blue.save()
        statement_records.clear()

        await account.tags.add(red, blue)
        added = _sent(statement_records)
        statement_records.clear()
        await account.tags.remove(red)
        removed = _sent(statement_records)
        statement_records.clear()
        await account.tags.clear()
        cleared = _sent(statement_records)

    assert added and {r.operation for r in added} == {"m2m_add"}
    assert [r.operation for r in removed] == ["m2m_remove"]
    assert [r.operation for r in cleared] == ["m2m_clear"]
    assert removed[0].rows == 1
    for record in added + removed + cleared:
        _assert_shared(record, db_backend)


@pytest.mark.asyncio
async def test_raw_statements_are_raw(db_url, db_backend, statement_records):
    await _connect(db_url)
    ph = _ph(db_backend)
    async with engines.session():
        await SlogTag(id=1, name="a").save()
        statement_records.clear()

        await raw.execute(f"UPDATE slogtag SET name = {ph} WHERE id = 1", "b")
        await raw.fetch_all("SELECT name FROM slogtag")
        await raw.fetch_one(f"SELECT name FROM slogtag WHERE id = {ph}", 1)

    execute, fetch_all, fetch_one = statement_records
    assert {r.operation for r in statement_records} == {"raw"}
    assert execute.params == ("b",)
    assert (execute.rows, fetch_all.rows, fetch_one.rows) == (1, None, None)
    for record in statement_records:
        _assert_shared(record, db_backend)


@pytest.mark.asyncio
async def test_a_nested_transaction_logs_its_control_statements_in_order(
    db_url, db_backend, statement_records
):
    await _connect(db_url)
    async with engines.session():
        await SlogTag(id=99, name="warm").save()
        statement_records.clear()

        async with transaction():
            await SlogTag(id=1, name="outer").save()
            async with transaction():
                await SlogTag(id=2, name="inner").save()
            with pytest.raises(RuntimeError):
                async with transaction():
                    await SlogTag(id=3, name="undone").save()
                    raise RuntimeError("undo")
            await SlogTag(id=4, name="after").save()

    seen = [(r.operation, r.sql.split()[0]) for r in _sent(statement_records)]
    assert seen == [
        ("transaction", "BEGIN"),
        ("save", "INSERT"),
        ("transaction", "SAVEPOINT"),
        ("save", "INSERT"),
        ("transaction", "RELEASE"),
        ("transaction", "SAVEPOINT"),
        ("save", "INSERT"),
        ("transaction", "ROLLBACK"),
        ("transaction", "RELEASE"),
        ("save", "INSERT"),
        ("transaction", "COMMIT"),
    ]
    rollback_to = _sent(statement_records)[7]
    assert rollback_to.sql.startswith("ROLLBACK TO SAVEPOINT")
    for record in _sent(statement_records):
        _assert_shared(record, db_backend)


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_session_settings_are_settings_records_inside_the_operations_transaction(
    db_url, statement_records
):
    await _connect(db_url)
    async with engines.session():
        await SlogTag(id=99, name="warm").save()
    statement_records.clear()

    async with engines.session(settings={TENANT_KEY: "acme"}):
        await SlogTag(id=1, name="scoped").save()

    seen = [(r.operation, r.sql.split()[0]) for r in _sent(statement_records)]
    begin = seen.index(("save", "BEGIN"))
    insert = seen.index(("save", "INSERT"))
    commit = seen.index(("save", "COMMIT"))
    settings = [i for i, (operation, _) in enumerate(seen) if operation == "settings"]
    assert settings and all(begin < i < insert < commit for i in settings)
    for index in settings:
        record = _sent(statement_records)[index]
        assert record.connection == "default"
        assert record.dialect == "postgres"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_the_catalog_probe_is_logged_on_a_tables_first_touch_only(
    db_url, statement_records
):
    await _connect(db_url)
    statement_records.clear()
    async with engines.session():
        await SlogTag.where(lambda tag: tag.id == 1).all()
        first = list(statement_records)
        statement_records.clear()
        await SlogTag.where(lambda tag: tag.id == 1).all()
        second = list(statement_records)

    assert [r.operation for r in first] == ["catalog", "fetch"]
    assert first[0].params == ("slogtag",)
    assert first[0].connection == "default"
    assert [r.operation for r in second] == ["fetch"]


# -- redaction ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_field_values_are_redacted_on_save_and_update(
    db_url, statement_records
):
    await _connect(db_url)
    async with engines.session():
        await _ada().save()
        fetched = await SlogAccount.get(1)
        fetched.token = SecretStr("sk_live_2")
        await fetched.save()

    insert, _, update = _sent(statement_records)
    assert [p for p in insert.params if p is REDACTED] == [REDACTED, REDACTED]
    assert "ada@example.com" in insert.params
    assert REDACTED in update.params
    _assert_hidden(statement_records, "sk_live_1", "sk_live_2", b"k\x00\xff")


@pytest.mark.asyncio
async def test_secret_field_values_are_redacted_on_bulk_create(
    db_url, statement_records
):
    await _connect(db_url)
    async with engines.session():
        await SlogAccount.bulk_create(
            [
                SlogAccount(id=1, email="a@example.com", token=SecretStr("sk_a")),
                SlogAccount(id=2, email="b@example.com", token=SecretStr("sk_b")),
            ]
        )

    (insert,) = _sent(statement_records)
    assert insert.operation == "bulk_create"
    assert insert.params.count(REDACTED) == 2
    _assert_hidden(statement_records, "sk_a", "sk_b")


@pytest.mark.asyncio
async def test_a_plain_value_equal_to_a_secret_of_the_same_operation_is_masked(
    db_url, statement_records
):
    """The false positive runs in the safe direction: a value that happens to
    equal a secret of the same operation is masked with it."""
    await _connect(db_url)
    async with engines.session():
        await SlogAccount.bulk_create(
            [
                SlogAccount(
                    id=1, email="a@example.com", token=SecretStr("b@example.com")
                ),
                SlogAccount(id=2, email="b@example.com", token=SecretStr("sk_b")),
            ]
        )

    (insert,) = _sent(statement_records)
    assert insert.params.count(REDACTED) == 3
    assert "a@example.com" in insert.params
    _assert_hidden(statement_records, "b@example.com", "sk_b")


@pytest.mark.asyncio
async def test_query_update_redacts_secret_and_plain_values_for_a_secret_column(
    db_url, statement_records
):
    await _connect(db_url)
    async with engines.session():
        await _ada().save()
        statement_records.clear()

        await SlogAccount.where(lambda account: account.id == 1).update(
            token=SecretStr("sk_live_2")
        )
        await SlogAccount.where(lambda account: account.id == 1).update(
            token="sk_live_3"
        )

    first, second = _sent(statement_records)
    assert first.params[0] is REDACTED
    assert second.params[0] is REDACTED
    assert 1 in second.params
    _assert_hidden(statement_records, "sk_live_2", "sk_live_3")


@pytest.mark.asyncio
async def test_a_plain_literal_against_a_secret_column_is_redacted(
    db_url, statement_records
):
    await _connect(db_url)
    async with engines.session():
        await _ada().save()
        statement_records.clear()

        found = await SlogAccount.where(
            lambda account: account.token == "sk_live_1"
        ).first()
        count = await SlogNote.where(
            lambda note: note.account.token == "sk_live_1"
        ).count()
        await SlogAccount.where(lambda account: account.token == "sk_live_1").delete()

    assert found is not None and count == 0
    for record in _sent(statement_records):
        assert REDACTED in record.params
    _assert_hidden(statement_records, "sk_live_1")


@pytest.mark.asyncio
async def test_a_secret_literal_against_a_plain_column_is_redacted(
    db_url, statement_records
):
    await _connect(db_url)
    async with engines.session():
        await _ada().save()
        statement_records.clear()

        found = await SlogAccount.where(
            lambda account: account.email == SecretStr("ada@example.com")
        ).first()

    assert found is not None
    (record,) = _sent(statement_records)
    assert record.params[0] is REDACTED
    _assert_hidden(statement_records, "ada@example.com")


@pytest.mark.asyncio
async def test_a_secret_raw_argument_is_redacted(db_url, db_backend, statement_records):
    await _connect(db_url)
    ph = _ph(db_backend)
    async with engines.session():
        await _ada().save()
        statement_records.clear()

        await raw.execute(
            f"UPDATE slogaccount SET token = {ph}", SecretStr("sk_live_2")
        )
        row = await raw.fetch_one(
            f"SELECT token FROM slogaccount WHERE token = {ph}", SecretStr("sk_live_2")
        )
        await raw.fetch_all(f"SELECT id FROM slogaccount WHERE id = {ph}", 1)

    assert row == {"token": "sk_live_2"}
    execute, fetch_one, fetch_all = statement_records
    assert execute.params == (REDACTED,)
    assert fetch_one.params == (REDACTED,)
    assert fetch_all.params == (1,)
    _assert_hidden(statement_records, "sk_live_2")


@pytest.mark.asyncio
async def test_with_the_log_off_no_operation_emits_or_collects_secrets(
    db_url, db_backend, monkeypatch
):
    import ferro.models
    import ferro.raw

    calls: list[dict] = []
    collected: list[str] = []
    monkeypatch.setattr(ferro._sql_log, "emit", calls.append)
    for module, name in (
        (ferro.models, "secret_values"),
        (ferro.raw, "secret_values_of"),
    ):
        original = getattr(module, name)

        def counting(*args, _original=original, _name=name, **kwargs):
            collected.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, name, counting)
    logger = logging.getLogger("ferro.sql")
    level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        await _connect(db_url)
        async with engines.session():
            account = _ada()
            await account.save()
            account.token = SecretStr("sk_live_2")
            await account.save()
            await SlogAccount.bulk_create(
                [SlogAccount(id=2, email="b@example.com", token=SecretStr("sk_b"))]
            )
            await SlogAccount.where(lambda account: account.token == "sk_b").count()
            await raw.execute(
                f"UPDATE slogaccount SET token = {_ph(db_backend)}",
                SecretStr("sk_live_3"),
            )
            async with transaction():
                await SlogTag(id=1, name="a").save()
    finally:
        logger.setLevel(level)

    assert calls == []
    assert collected == []


@pytest.mark.asyncio
async def test_a_failed_statement_is_logged_with_its_error_and_still_raises(
    db_url, db_backend, statement_records
):
    await _connect(db_url)
    async with engines.session():
        await _ada().save()
        statement_records.clear()

        with pytest.raises(IntegrityError):
            await SlogAccount(
                id=2, email="ada@example.com", token=SecretStr("sk_live_9")
            ).save()

    (failed,) = _sent(statement_records)
    assert failed.operation == "save"
    assert failed.error is not None
    assert failed.getMessage().endswith(f" failed: {failed.error}")
    _assert_hidden(statement_records, "sk_live_9")
