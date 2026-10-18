# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""Foreign defaults on every door (#628, ADR-0027 / ADR-0031 / ADR-0034 as
amended).

Someone sets a server default by hand that no model declares:

```sql
ALTER TABLE "author" ALTER COLUMN "slug" SET DEFAULT '';
```

```text
$ ferro migrate drift
drift against 0001_create_author:
  author.slug has default '' in the database, none declared
```

``baseline`` refuses with the same line; ``connect(migrate_destructive=True)``
drops it (``ALTER TABLE "author" ALTER COLUMN "slug" DROP DEFAULT``) and
``migrate_updates`` leaves it and warns; Alembic autogenerate writes
``op.alter_column(..., server_default=None)`` with an irreversible
downgrade. Ferro's own defaults are never drift: a ``serial`` key's
sequence on Postgres, and on SQLite a literal on a ``NOT NULL`` column (the
pass's kept default). Every test runs against a real database, on SQLite and
Postgres.
"""

from __future__ import annotations

import asyncio
import importlib
import re
import sys

import pytest

import ferro
from ferro import PassReport, _core
from ferro.migrations import baseline
from ferro.migrations._drift import against
from tests._pass_harness import auto_migrate, schema_sql, warned_auto_migrate
from tests.test_migrate_drift import PLAIN_TEAM, snapshot_of
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    pkg,
    project,
    run,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401
    configure,
    db,
    new,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

SLUGGED = AUTHOR + '    slug: str = ""\n    bio: str | None = None\n'
DROP_SLUG_DEFAULT = 'ALTER TABLE "author" ALTER COLUMN "slug" DROP DEFAULT'
SLUG_LINE = "author.slug has default '' in the database, none declared"
NOT_COMPARED = (
    "baseline checked what `ferro migrate drift` checks: objects ferro does not "
    "own (functions, triggers) were not compared"
)


def cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = run("migrate", *argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def generated(project, pkg, db, models: str = SLUGGED) -> None:
    """``0001_create_author`` from ``models``, and those models registered."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, models)
    new("create_author")


def register(project, pkg, body: str) -> None:
    """Write ``body`` as the models and import them, as an app's start-up does."""
    write_models(project, pkg, body)
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))
    importlib.import_module(f"{pkg}.models")


def passed(url: str, *, warned: bool = False, **flags: bool) -> PassReport:
    """One auto-migrate pass over ``url`` with ``flags``, its connection
    closed after; ``warned`` captures its warnings (``warned_auto_migrate``)."""

    async def run_pass() -> PassReport:
        door = warned_auto_migrate if warned else auto_migrate
        report = await door(url, name="pass_628", **flags)
        await _core._disconnect("pass_628")
        return report

    return asyncio.run(run_pass())


def built_by_the_pass(db) -> None:
    """The models' tables, as ``connect(auto_migrate=True)`` builds them."""
    passed(db.url)


def sqlite_set_default(db, table: str, column: str, default: str) -> None:
    """Give ``table.column`` the server default ``default`` on SQLite, which
    has no ``ALTER COLUMN … SET DEFAULT``: the table is rebuilt by hand from
    its own ``CREATE TABLE`` with the clause added, its indexes put back."""
    (create,) = db.rows(
        f"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = '{table}'"
    )[0]
    indexes = [
        row[0]
        for row in db.rows(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            f"AND tbl_name = '{table}' AND sql IS NOT NULL"
        )
    ]
    pattern = re.compile(rf'("{column}" [^,)]*?)(?=,|\)$| NOT NULL| CHECK)')
    edited, found = pattern.subn(lambda m: f"{m[1]} DEFAULT {default}", create, 1)
    assert found == 1, create
    script = [
        "PRAGMA foreign_keys = OFF",
        f'ALTER TABLE "{table}" RENAME TO "_hand_old"',
        edited,
        f'INSERT INTO "{table}" SELECT * FROM "_hand_old"',
        'DROP TABLE "_hand_old"',
        *indexes,
    ]
    conn = db._connect()
    try:
        for statement in script:
            conn.execute(statement)
    finally:
        conn.close()


def set_default(db, table: str, column: str, default: str) -> None:
    """The hand edit: ``table.column`` gets the server default ``default``."""
    if db.backend == "postgres":
        db.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT {default}'
        )
    else:
        sqlite_set_default(db, table, column, default)


def drift_lines(project, db, name: str = "0001_create_author") -> list[str]:
    """What ``drift`` / ``baseline`` would print against migration ``name``'s
    snapshot, read without records."""

    async def check() -> list[str]:
        await ferro.connect(db.url, name="drift_probe")
        try:
            report = await against(
                snapshot_of(project, name), migration=name, using="drift_probe"
            )
            return report.lines
        finally:
            await _core._disconnect("drift_probe")

    return asyncio.run(check())


# -- a hand-set default (Postgres) --------------------------------------------------


def test_a_hand_set_default_is_drift_with_its_own_line_and_exit_4(
    project, pkg, db, capsys
):
    if db.backend != "postgres":
        pytest.skip("SQLite cannot set a default in place; see the SQLite tests")
    generated(project, pkg, db)
    assert run("migrate", "up", "--url", db.url) == 0
    set_default(db, "author", "slug", "''")
    capsys.readouterr()

    code, out, _ = cli(capsys, "drift", "--url", db.url)

    assert code == 4
    assert out == (
        f"{db.line}\nxx 1 difference against 0001_create_author\n  - {SLUG_LINE}\n"
    )
    report = asyncio.run(ferro.migrations.drift(url=db.url))
    assert report.lines == [SLUG_LINE]
    assert report.operations[0]["kind"] == "DropDefault"
    assert report.operations[0]["was"] == "''"


def test_baseline_refuses_a_hand_set_default_and_records_nothing(
    project, pkg, db, capsys
):
    if db.backend != "postgres":
        pytest.skip("SQLite cannot set a default in place; see the SQLite tests")
    generated(project, pkg, db)
    built_by_the_pass(db)
    set_default(db, "author", "slug", "''")
    capsys.readouterr()

    code, out, _ = cli(capsys, "baseline", "--url", db.url)

    assert code == 4
    assert out == (
        f"{db.line}\n"
        f"drift against 0001_create_author:\n  {SLUG_LINE}\nnothing was recorded\n"
    )
    assert "_ferro_migrations" not in db.tables() or db.records() == []
    report = asyncio.run(baseline(url=db.url))
    assert report.recorded == []
    assert report.drift is not None and report.drift.lines == [SLUG_LINE]


def test_migrate_destructive_drops_it_and_reports_the_statement(
    project, pkg, db, capsys
):
    if db.backend != "postgres":
        pytest.skip("SQLite cannot set a default in place; see the SQLite tests")
    generated(project, pkg, db)
    built_by_the_pass(db)
    set_default(db, "author", "slug", "''")

    report = passed(db.url, destructive=True)

    assert schema_sql(report) == [DROP_SLUG_DEFAULT]
    assert report.warnings == ()
    assert drift_lines(project, db) == []
    capsys.readouterr()
    code, out, _ = cli(capsys, "baseline", "--url", db.url)
    assert code == 0, out
    assert out.endswith(NOT_COMPARED + "\n")


def test_migrate_updates_leaves_it_and_warns_naming_the_fix(project, pkg, db):
    if db.backend != "postgres":
        pytest.skip("SQLite cannot set a default in place; see the SQLite tests")
    generated(project, pkg, db)
    built_by_the_pass(db)
    set_default(db, "author", "slug", "''")

    report = passed(db.url, warned=True, updates=True)

    assert schema_sql(report) == []
    [warning] = report.warnings
    assert warning.kind == "ForeignDefault"
    assert (warning.subject.table, warning.subject.column) == ("author", "slug")
    assert warning.text == (
        "author.slug has default '' in the database, none declared; it is left in "
        "place: connect(..., migrate_destructive=True) drops it, or run ALTER TABLE "
        '"author" ALTER COLUMN "slug" DROP DEFAULT.'
    )
    assert drift_lines(project, db) == [SLUG_LINE]


def test_autogenerate_drops_it_with_alembics_alter_column_and_cannot_put_it_back(
    project, pkg, db
):
    if db.backend != "postgres":
        pytest.skip(
            "SQLite drops a default only by a rebuild, which a revision refuses"
        )
    from tests._alembic_harness import autogenerate, run_revision

    generated(project, pkg, db)
    built_by_the_pass(db)
    set_default(db, "author", "slug", "''")

    upgrade, downgrade = autogenerate(db.url, db.base, db.schema)

    flat = re.sub(r"\s+", " ", upgrade)
    assert (
        "op.alter_column('author', 'slug', existing_type=sa.String(), "
        "server_default=None, existing_nullable=False)" in flat
    ), upgrade
    assert "DROP DEFAULT" not in upgrade, upgrade
    assert "raise RuntimeError(" in downgrade, downgrade
    assert "set outside ferro" in downgrade, downgrade
    assert "op.alter_column" not in downgrade, downgrade
    run_revision(upgrade, db.url, db.base, db.schema)
    assert drift_lines(project, db) == []


def test_autogenerate_on_sqlite_refuses_naming_the_rebuild(project, pkg, db):
    if db.backend != "sqlite":
        pytest.skip("Postgres drops a default in place")
    from tests._alembic_harness import autogenerate

    generated(project, pkg, db)
    built_by_the_pass(db)
    set_default(db, "author", "bio", "(datetime('now'))")

    with pytest.raises(Exception, match=r"DropDefault .*ferro migrate new"):
        autogenerate(db.url, db.base, db.schema)


# -- ferro's own defaults -------------------------------------------------------------


def test_a_fresh_tables_key_sequence_is_never_drift_where_up_built_it(
    project, pkg, db, capsys
):
    generated(project, pkg, db, PLAIN_TEAM)
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()
    assert cli(capsys, "drift", "--url", db.url)[:2] == (
        0,
        f"{db.line}\nno drift against 0001_create_author\n",
    )


def test_a_fresh_tables_key_sequence_is_never_drift_where_the_pass_built_it(
    project, pkg, db, capsys
):
    generated(project, pkg, db, PLAIN_TEAM)
    built_by_the_pass(db)
    assert drift_lines(project, db) == []
    capsys.readouterr()
    code, out, _ = cli(capsys, "baseline", "--url", db.url)
    assert code == 0, out
    assert drift_lines(project, db) == []


def test_a_renamed_tables_sequence_is_never_drift_on_a_pass_built_database(
    project, pkg, db
):
    """The pass renames ``team`` to ``squad`` (and, on Postgres, the key's
    sequence with it); a second destructive pass finds nothing to do, so
    the sequence's default is recognised as ferro's own."""
    configure(project, pkg, db.backend)
    register(project, pkg, PLAIN_TEAM)
    built_by_the_pass(db)
    register(
        project,
        pkg,
        PLAIN_TEAM.replace(
            "class Team(Model):\n",
            'class Squad(Model):\n    __ferro_renamed_from__ = "team"\n',
        ),
    )
    renamed = passed(db.url, updates=True)
    assert any("RENAME TO" in sql for sql in schema_sql(renamed)), schema_sql(renamed)

    again = passed(db.url, destructive=True)
    assert schema_sql(again) == []
    assert again.warnings == ()


@pytest.mark.parametrize(
    ("field", "kept"), [('tier: str = "free"', "'free'"), ("rank: int = 0", "0")]
)
def test_sqlites_kept_literal_is_never_drift_and_baseline_records(
    project, pkg, db, capsys, field, kept
):
    """The pass adds a required column with a literal default as ``NOT NULL
    DEFAULT <literal>`` and keeps it (ADR-0034): by shape, ferro's own."""
    if db.backend != "sqlite":
        pytest.skip("only SQLite keeps a default (ADR-0034's SQLite exception)")
    configure(project, pkg, db.backend)
    register(project, pkg, AUTHOR)
    built_by_the_pass(db)
    register(project, pkg, AUTHOR + f"    {field}\n")
    passed(db.url, updates=True)
    column = field.split(":")[0]
    assert [
        row[4] for row in db.rows('PRAGMA table_info("author")') if row[1] == column
    ] == [kept]
    new("create_author")
    capsys.readouterr()

    code, out, _ = cli(capsys, "baseline", "--url", db.url)

    assert code == 0, out
    assert out == (
        f"{db.line}\nrecorded 0001_create_author as baseline (1 step)\n"
        + NOT_COMPARED
        + "\n"
    )


# -- SQLite's foreign defaults --------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "default", "line"),
    [
        (
            "slug",
            "(datetime('now'))",
            "author.slug has default datetime('now') in the database, none declared",
        ),
        ("bio", "''", "author.bio has default '' in the database, none declared"),
    ],
)
def test_sqlite_an_expression_or_a_nullable_columns_default_is_drift(
    project, pkg, db, capsys, column, default, line
):
    if db.backend != "sqlite":
        pytest.skip("SQLite's shape rule")
    generated(project, pkg, db)
    built_by_the_pass(db)
    set_default(db, "author", column, default)

    assert drift_lines(project, db) == [line]
    capsys.readouterr()
    code, out, _ = cli(capsys, "baseline", "--url", db.url)
    assert code == 4
    assert out == (
        f"{db.line}\ndrift against 0001_create_author:\n  {line}\n"
        "nothing was recorded\n"
    )

    before = db.rows('PRAGMA table_info("author")')
    report = passed(db.url, warned=True, updates=True, destructive=True)
    assert schema_sql(report) == []
    [warning] = report.warnings
    assert warning.kind == "SqliteInPlace"
    assert "ferro migrate new" in warning.text, warning.text
    assert warning.text.startswith(line), warning.text
    assert db.rows('PRAGMA table_info("author")') == before


def test_a_successful_baseline_no_longer_says_defaults_were_not_compared(
    project, pkg, db, capsys
):
    generated(project, pkg, db)
    built_by_the_pass(db)
    capsys.readouterr()

    code, out, _ = cli(capsys, "baseline", "--url", db.url)

    assert code == 0
    assert out == (
        f"{db.line}\nrecorded 0001_create_author as baseline (1 step)\n"
        + NOT_COMPARED
        + "\n"
    )
    assert "column defaults" not in out
