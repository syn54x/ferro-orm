# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""The DDL lock timeout (#522, ADR-0044).

A long query holds a lock on ``author``; a migration's ``ALTER TABLE
"author"`` queues behind it, and every new query on ``author`` would queue
behind the ``ALTER``. Under ``ddl_lock_timeout`` the ``ALTER`` gives up
instead, and the step is tried again from its first statement::

    $ ferro migrate up
    0003_add_slug  01_add_slug  waiting for a lock on "author" (attempt 1 of 10, retry in 1s)
    0003_add_slug  01_add_slug  waiting for a lock on "author" (attempt 2 of 10, retry in 2s)
    0003_add_slug  01_add_slug  applied (5214 ms)

The behaviour is Postgres-only; on SQLite every case asserts that nothing is
set and the statements are what they were.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Annotated, Any

import pytest

import ferro
from ferro import Model
from ferro.base import FerroField
from ferro.exceptions import OperationalError
from ferro.migrations import runner
from tests._pass_harness import auto_migrate
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    pkg,
    project,
    run,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    new,
    run_report,
    settings_and_database,
    sql_step,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
    pytest.mark.asyncio,
]

ADD_SLUG = (
    'INSERT INTO "attempt_log" VALUES (1);\n'
    'ALTER TABLE "author" ADD COLUMN "slug" text;\n'
)


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def ferro_log():
    """Every message on the ``ferro`` logger (which does not propagate to
    pytest's capture), as executed statements are logged before they run."""
    logger = logging.getLogger("ferro")
    handler = _Collect()
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def _postgres_only(db) -> None:
    if db.backend != "postgres":
        pytest.skip("the DDL lock timeout is Postgres-only")


class Holder:
    """Another session holding ``ACCESS EXCLUSIVE`` on a table (or, with
    ``sql``, whatever lock that statement takes, such as an uncommitted
    write), as a long query would hold a lock a DDL statement needs.
    Without ``seconds`` it holds until the ``with`` block ends, or
    ``SAFETY_HOLD`` seconds, so a statement that never gives up fails the
    test instead of hanging it."""

    SAFETY_HOLD = 30.0

    def __init__(
        self, db, table: str, seconds: float | None = None, sql: str | None = None
    ):
        self.db = db
        self.table = table
        self.seconds = seconds
        self.sql = sql or f'LOCK TABLE "{table}" IN ACCESS EXCLUSIVE MODE'
        self.locked = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        import psycopg

        conn = psycopg.connect(self.db.base)
        try:
            conn.execute(f'SET search_path TO "{self.db.schema}"')
            conn.execute(self.sql)
            self.locked.set()
            if self.seconds is None:
                self.release.wait(self.SAFETY_HOLD)
            else:
                self._await_a_waiter(conn)
                self.release.wait(self.seconds)
            conn.commit()
        finally:
            conn.close()

    def _await_a_waiter(self, conn) -> None:
        """Block until another session queues behind this lock on the table.

        ``seconds`` is how long the statement under test waits, so its clock
        starts when that statement first waits, not when the lock was taken:
        on a loaded machine (two runs of the suite against one server) the
        run's setup between the two can eat most of a short hold, and the
        statement would then find the lock already gone."""
        deadline = time.monotonic() + self.SAFETY_HOLD
        while not self.release.is_set() and time.monotonic() < deadline:
            waiting = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted "
                "AND relation = to_regclass(%s) AND pid <> pg_backend_pid())",
                (f'"{self.db.schema}"."{self.table}"',),
            ).fetchone()[0]
            if waiting:
                return
            time.sleep(0.02)

    def __enter__(self) -> Holder:
        self.thread.start()
        assert self.locked.wait(10), "the holder never took the lock"
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.thread.join(10)


async def _project(project, pkg, db, extra: str = ""):
    """``author`` and ``attempt_log`` applied; ``extra`` lands in ferro.toml."""
    configure(project, pkg, db.backend, extra)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_step(project, "attempt_log", 'CREATE TABLE "attempt_log" ("n" integer);\n')
    settings, database = settings_and_database()
    assert (await runner.up(settings, database.name, url=db.url)).refusal is None
    return settings, database


def _record(db, migration: int) -> list[tuple]:
    return db.rows(
        "SELECT finished_at, failed_at, error, duration_ms FROM _ferro_migrations "
        f"WHERE migration = {migration}"
    )


async def _up(settings, database, db) -> tuple[Any, list[str]]:
    lines: list[str] = []
    report = await run_report(
        runner.up(settings, database.name, url=db.url, progress=lines.append)
    )
    return report, lines


# -- a run ------------------------------------------------------------------------------


@pytest.mark.parametrize("no_transaction", [False, True], ids=["transaction", "none"])
async def test_a_step_behind_a_held_lock_is_retried_and_finishes_with_one_record(
    project, pkg, db, no_transaction
):
    _postgres_only(db)
    settings, database = await _project(project, pkg, db, 'ddl_lock_timeout = "1s"\n')
    header = "-- ferro: no-transaction\n" if no_transaction else ""
    sql_step(project, "add_slug", header + ADD_SLUG)

    with Holder(db, "author", seconds=3.5):
        running = asyncio.create_task(_up(settings, database, db))
        await asyncio.sleep(1.5)
        # Between attempts the record stands started: `status` sees a live run.
        assert await asyncio.to_thread(_record, db, 3) == [(None, None, None, 0)]
        report, lines = await running

    assert report.refusal is None
    assert lines[:2] == [
        '0003_add_slug  01_add_slug  waiting for a lock on "author" '
        "(attempt 1 of 10, retry in 1s)",
        '0003_add_slug  01_add_slug  waiting for a lock on "author" '
        "(attempt 2 of 10, retry in 2s)",
    ]
    assert lines[2].startswith("0003_add_slug  01_add_slug  applied (")
    [(finished, failed, error, duration_ms)] = _record(db, 3)
    assert finished is not None and failed is None and error is None
    # One record, its duration the whole step: every attempt and every wait.
    assert duration_ms >= 3000
    assert report.applied[0].ms >= duration_ms
    # A transaction rolled back each timed-out attempt; a no-transaction step
    # re-ran from its first statement on every attempt.
    assert db.rows('SELECT count(*) FROM "attempt_log"') == [
        (3 if no_transaction else 1,)
    ]
    assert "slug" in {
        row[0]
        for row in db.rows(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema = '{db.schema}' AND table_name = 'author'"
        )
    }


async def test_ten_timeouts_fail_the_step_naming_ddl_lock_timeout_and_up_resumes(
    project, pkg, db, monkeypatch, capsys
):
    _postgres_only(db)
    monkeypatch.setenv("FERRO_TEST_BACKOFF_CAP_MS", "50")
    settings, database = await _project(
        project, pkg, db, 'ddl_lock_timeout = "100ms"\n'
    )
    sql_step(project, "add_slug", ADD_SLUG)

    with Holder(db, "author"):
        report, lines = await _up(settings, database, db)

    assert report.refusal is not None
    assert (
        'lock on "author" not acquired within 100ms after 10 attempts; set '
        "ddl_lock_timeout under [tool.ferro]"
    ) in report.refusal
    assert report.refusal.startswith("ferro migrate: 0003_add_slug/")
    assert [line.split("  ", 2)[2] for line in lines] == [
        f'waiting for a lock on "author" (attempt {n} of 10, retry in 50ms)'
        for n in range(1, 10)
    ]
    [(finished, failed, error, _)] = _record(db, 3)
    assert finished is None and failed is not None and "ddl_lock_timeout" in error
    capsys.readouterr()
    assert await asyncio.to_thread(run, "migrate", "status", "--url", db.url) == 4
    assert "failed" in capsys.readouterr().out

    report, lines = await _up(settings, database, db)

    assert report.refusal is None
    assert len(lines) == 1 and "applied (" in lines[0]
    [(finished, failed, error, _)] = _record(db, 3)
    assert finished is not None and failed is None and error is None


async def test_a_down_waits_under_the_same_timeout(project, pkg, db):
    _postgres_only(db)
    settings, database = await _project(project, pkg, db, 'ddl_lock_timeout = "1s"\n')
    up_file = sql_step(project, "add_slug", ADD_SLUG)
    up_file.with_name("01_add_slug.down.sql").write_text(
        'ALTER TABLE "author" DROP COLUMN "slug";\n'
    )
    report, _ = await _up(settings, database, db)
    assert report.refusal is None

    lines: list[str] = []
    with Holder(db, "author", seconds=1.5):
        report = await runner.down(
            settings, database.name, url=db.url, progress=lines.append
        )

    assert report.refusal is None
    assert lines[0] == (
        '0003_add_slug  01_add_slug  waiting for a lock on "author" '
        "(attempt 1 of 10, retry in 1s)"
    )
    assert "reverted (" in lines[1]


@pytest.mark.parametrize(
    "extra, shown",
    [
        ("", "5s"),
        ('ddl_lock_timeout = "1s"\n', "1s"),
        ('ddl_lock_timeout = "0"\n', "0"),
    ],
    ids=["default", "configured", "disabled"],
)
@pytest.mark.parametrize("no_transaction", [False, True], ids=["transaction", "none"])
async def test_the_step_runs_under_the_configured_timeout_and_zero_sets_none(
    project, pkg, db, ferro_log, extra, shown, no_transaction
):
    _postgres_only(db)
    settings, database = await _project(project, pkg, db, extra)
    header = "-- ferro: no-transaction\n" if no_transaction else ""
    sql_step(
        project,
        "seen",
        header
        + "CREATE TABLE \"seen\" AS SELECT current_setting('lock_timeout') AS v;\n",
    )

    report, _ = await _up(settings, database, db)

    assert report.refusal is None
    assert db.rows('SELECT v FROM "seen"') == [(shown,)]
    if shown == "0":
        assert not [m for m in ferro_log if "SET" in m and "lock_timeout" in m]
    else:
        ms = {"5s": 5000, "1s": 1000}[shown]
        expected = (
            f"SET lock_timeout = '{ms}ms'"
            if no_transaction
            else f"SET LOCAL lock_timeout = '{ms}ms'"
        )
        assert any(m.endswith(expected) for m in ferro_log)
        assert any(m.endswith("RESET lock_timeout") for m in ferro_log) == (
            no_transaction
        )


async def test_zero_waits_for_the_lock_without_a_retry(project, pkg, db):
    _postgres_only(db)
    settings, database = await _project(project, pkg, db, 'ddl_lock_timeout = "0"\n')
    sql_step(project, "add_slug", ADD_SLUG)

    with Holder(db, "author", seconds=1.5):
        report, lines = await _up(settings, database, db)

    assert report.refusal is None
    assert len(lines) == 1 and "applied (" in lines[0]
    assert _record(db, 3)[0][3] >= 1000


async def test_sqlite_sets_nothing_and_runs_the_file_as_written(
    project, pkg, db, ferro_log
):
    if db.backend != "sqlite":
        pytest.skip("the SQLite half")
    settings, database = await _project(project, pkg, db, 'ddl_lock_timeout = "1s"\n')
    sql_step(project, "add_slug", ADD_SLUG)

    report, _ = await _up(settings, database, db)

    assert report.refusal is None
    executed = [m for m in ferro_log if m.startswith("ferro migrate: 0003_")]
    assert executed == [
        'ferro migrate: 0003_add_slug/01_add_slug.up.sql: INSERT INTO "attempt_log" VALUES (1)',
        'ferro migrate: 0003_add_slug/01_add_slug.up.sql: ALTER TABLE "author" ADD COLUMN "slug" text',
    ]
    assert not [m for m in ferro_log if "lock_timeout" in m]


# -- the reconciliation pass ---------------------------------------------------------------


def _live_author(db) -> None:
    db.execute('CREATE TABLE "author" ("id" integer PRIMARY KEY, "name" text NOT NULL)')


def _declare_author() -> None:
    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        slug: str | None = None

    del Author


def _ran(report: ferro.PassReport) -> list[tuple[str, str, str]]:
    """Every statement the pass sent, as ``(subject, role, sql)``, in order."""
    return [(s.subject, s.role, s.sql) for s in report.statements]


@pytest.mark.parametrize(
    "extra, ms", [(None, 5000), ('ddl_lock_timeout = "1s"\n', 1000)], ids=["none", "1s"]
)
async def test_the_pass_runs_every_table_statement_under_set_local(
    project, pkg, db, extra, ms
):
    if extra is not None:
        configure(project, pkg, db.backend, extra)
    _live_author(db)
    _declare_author()

    report = await auto_migrate(db.url, updates=True)

    if db.backend == "sqlite":
        assert _ran(report) == [
            ("author", "schema", 'ALTER TABLE "author" ADD COLUMN "slug" varchar')
        ]
        return
    # One SET LOCAL, first in the table's one transaction, reported as the
    # executor's own statement (role "lock_timeout"), never as schema.
    assert _ran(report) == [
        ("author", "lock_timeout", f"SET LOCAL lock_timeout = '{ms}ms'"),
        ("author", "schema", 'ALTER TABLE "author" ADD COLUMN "slug" varchar'),
        (
            "author",
            "schema",
            'ALTER TABLE "author" ALTER COLUMN "name" TYPE varchar USING "name"::varchar',
        ),
    ]


async def test_the_pass_retries_behind_a_held_lock_and_warns_each_attempt(
    project, pkg, db
):
    _postgres_only(db)
    configure(project, pkg, db.backend, 'ddl_lock_timeout = "1s"\n')
    _live_author(db)
    _declare_author()

    with Holder(db, "author", seconds=1.5), pytest.warns(UserWarning) as caught:
        report = await auto_migrate(db.url, updates=True)

    waiting = [str(w.message) for w in caught if "waiting for a lock" in str(w.message)]
    assert waiting == [
        "ferro auto-migrate: migrating 'author': waiting for a lock on \"author\" "
        "(attempt 1 of 10, retry in 1s)"
    ]
    # The report lists the retry as a warning, and only the attempt that
    # succeeded among the statements.
    assert [(w.kind, w.subject.table, str(w)) for w in report.warnings] == [
        (
            "DdlLockRetry",
            "author",
            "migrating 'author': waiting for a lock on \"author\" "
            "(attempt 1 of 10, retry in 1s)",
        )
    ]
    assert _ran(report) == [
        ("author", "lock_timeout", "SET LOCAL lock_timeout = '1000ms'"),
        ("author", "schema", 'ALTER TABLE "author" ADD COLUMN "slug" varchar'),
        (
            "author",
            "schema",
            'ALTER TABLE "author" ALTER COLUMN "name" TYPE varchar USING "name"::varchar',
        ),
    ]
    assert "slug" in {
        row[0]
        for row in db.rows(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema = '{db.schema}' AND table_name = 'author'"
        )
    }


async def test_the_pass_fails_loudly_naming_ddl_lock_timeout(
    project, pkg, db, monkeypatch
):
    _postgres_only(db)
    monkeypatch.setenv("FERRO_TEST_BACKOFF_CAP_MS", "50")
    configure(project, pkg, db.backend, 'ddl_lock_timeout = "100ms"\n')
    _live_author(db)
    _declare_author()

    with Holder(db, "author"), pytest.warns(UserWarning):
        with pytest.raises(OperationalError) as exc:
            await ferro.connect(db.url, migrate_updates=True)

    # connect() carries the report too: nine retries said, nothing committed.
    assert [w.kind for w in exc.value.report.warnings] == ["DdlLockRetry"] * 9
    assert exc.value.report.statements == ()
    assert "after 10 attempts" in str(exc.value)
    assert "ddl_lock_timeout" in str(exc.value)
    assert exc.value.sqlstate == "55P03"


async def test_a_failed_no_transaction_step_resets_the_timeout_on_its_connection(
    project, pkg, db
):
    _postgres_only(db)
    from ferro.raw import fetch_all

    settings, database = await _project(project, pkg, db, 'ddl_lock_timeout = "1s"\n')
    sql_step(
        project,
        "broken",
        '-- ferro: no-transaction\nSELECT 1;\nINSERT INTO "missing" VALUES (1);\n',
    )
    # One pooled connection: the step's, read back after it failed.
    await ferro.connect(db.url, name="one", pool=ferro.PoolConfig(max_connections=1))

    report = await run_report(runner.up(settings, database.name, using="one"))

    assert report.refusal is not None and '"missing"' in report.refusal
    rows = await fetch_all("SELECT current_setting('lock_timeout') AS v", using="one")
    assert rows == [{"v": "0"}]


async def test_a_progress_callback_that_raises_is_raised_after_the_step_settles(
    project, pkg, db
):
    _postgres_only(db)
    settings, database = await _project(project, pkg, db, 'ddl_lock_timeout = "1s"\n')
    sql_step(project, "add_slug", ADD_SLUG)

    def progress(line: str) -> None:
        if "waiting for a lock" in line:
            raise RuntimeError("the progress sink broke")

    with Holder(db, "author", seconds=1.5), pytest.raises(RuntimeError, match="sink"):
        await runner.up(settings, database.name, url=db.url, progress=progress)

    [(finished, failed, error, _)] = _record(db, 3)
    assert finished is not None and failed is None and error is None


# -- the create pass ----------------------------------------------------------------------


def _live_parent(db) -> None:
    db.execute('CREATE TABLE "parent" ("id" integer PRIMARY KEY, "name" text NOT NULL)')


PARENT_AND_CHILD = """
class Parent(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    children: Relation[list["Child"]] = BackRef()


class Child(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    parent: Annotated[Parent, ForeignKey(related_name="children")]
"""


def _declare_parent_and_child(project, pkg) -> None:
    """``Parent`` (live already) and a new ``Child`` referencing it, imported
    from a module so the forward references resolve."""
    import importlib
    import sys

    write_models(project, pkg, PARENT_AND_CHILD)
    sys.path.insert(0, str(project))
    importlib.import_module(f"{pkg}.models")


OPEN_WRITE = 'INSERT INTO "parent" ("id", "name") VALUES (1, \'held\')'


async def test_a_new_table_referencing_a_written_parent_retries_then_is_created(
    project, pkg, db
):
    _postgres_only(db)
    configure(project, pkg, db.backend, 'ddl_lock_timeout = "1s"\n')
    _live_parent(db)
    _declare_parent_and_child(project, pkg)

    # An open write on `parent` conflicts with the lock `REFERENCES "parent"`
    # takes: the CREATE TABLE gives up instead of queueing every query on it.
    with (
        Holder(db, "parent", seconds=1.5, sql=OPEN_WRITE),
        pytest.warns(UserWarning) as caught,
    ):
        report = await auto_migrate(db.url)

    waiting = [str(w.message) for w in caught if "waiting for a lock" in str(w.message)]
    assert waiting == [
        "ferro auto-migrate: migrating 'child': waiting for a lock on \"parent\" "
        "(attempt 1 of 10, retry in 1s)"
    ]
    assert "child" in db.tables()
    assert [s.sql for s in report.statements if s.role == "lock_timeout"] == [
        "SET LOCAL lock_timeout = '1000ms'"
    ]
    assert [w.kind for w in report.warnings] == ["DdlLockRetry"]


async def test_zero_creates_the_new_table_once_the_write_commits_without_a_set(
    project, pkg, db
):
    _postgres_only(db)
    configure(project, pkg, db.backend, 'ddl_lock_timeout = "0"\n')
    _live_parent(db)
    _declare_parent_and_child(project, pkg)

    with Holder(db, "parent", seconds=1.5, sql=OPEN_WRITE):
        report = await auto_migrate(db.url)

    assert "child" in db.tables()
    assert [s for s in report.statements if s.role == "lock_timeout"] == []
    assert report.warnings == ()


# -- which database's timeout ------------------------------------------------------------


def _two_databases(project, pkg, a: str, b: str) -> None:
    (project / "ferro.toml").write_text(
        f'[databases.a]\nmodels = ["{pkg}.a"]\ndialects = ["sqlite", "postgres"]\n'
        f'ddl_lock_timeout = "{a}"\n\n'
        f'[databases.b]\nmodels = ["{pkg}.b"]\ndialects = ["sqlite", "postgres"]\n'
        f'ddl_lock_timeout = "{b}"\n'
    )


async def test_databases_that_disagree_on_the_timeout_are_refused_naming_each(
    project, pkg, db
):
    from ferro.settings import SettingsError

    _two_databases(project, pkg, "0", "30s")
    _declare_author()

    with pytest.raises(SettingsError) as refused:
        await ferro.connect(db.url, migrate_updates=True)

    message = str(refused.value)
    assert '`a` = "0"' in message and '`b` = "30s"' in message
    assert (
        "set the same ddl_lock_timeout on every database, or configure one database"
    ) in message
    assert "author" not in db.tables()


async def test_databases_that_agree_on_the_timeout_run_under_it(project, pkg, db):
    _two_databases(project, pkg, "2s", "2s")
    _live_author(db)
    _declare_author()

    report = await auto_migrate(db.url, updates=True)

    timeouts = [s.sql for s in report.statements if s.role == "lock_timeout"]
    if db.backend == "postgres":
        assert timeouts == ["SET LOCAL lock_timeout = '2000ms'"]
    else:
        assert timeouts == []
