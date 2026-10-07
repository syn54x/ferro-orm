# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate down`` (#520): generated downs and walking back.

```text
$ ferro migrate down
Revert 0002_add_teams (1 step)? [y/N] y
0002_add_teams  01_schema  reverted (8 ms)
```

Every test builds a real project under ``tmp_path``, applies its migrations
with ``ferro migrate up`` against the parametrized database (SQLite and
Postgres), walks back with ``down`` and reads the tracking table and the
live schema back the way an operator would. A down reaches its parent
snapshot: the one planner, run from the live schema to the parent, plans
nothing (ADR-0033).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

import ferro
from ferro import _core
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    LIBRARY,
    isolated_imports,
    pkg,
    project,
    run,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    migrations,
    new,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

TAG = """
class Tag(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
"""


# -- helpers ------------------------------------------------------------------------


def keys(db) -> list[tuple[int, int]]:
    return [(r[0], r[1]) for r in db.records()]


def migration_dir(project: Path, number: int) -> Path:
    return next(migrations(project).glob(f"{number:04}_*"))


def snapshot(project: Path, number: int) -> dict:
    return json.loads((migration_dir(project, number) / "ir.json").read_text())


def empty_snapshot(project: Path) -> dict:
    """The modelset with no models: the parent of ``0001``."""
    ir = snapshot(project, 1)
    return {**ir, "payload": {**ir["payload"], "models": []}}


def tables_of(ir: dict) -> set[str]:
    return {model["table_name"] for model in ir["payload"]["models"]}


def plan_against(db, parent: dict, child: dict) -> list[dict]:
    """The one planner's operations from the live schema to ``parent``, over
    every table either snapshot names (so a table the down left behind shows
    up as a drop, and one it failed to restore as an add)."""

    async def read() -> list[dict]:
        name = f"down_check_{uuid.uuid4().hex}"
        await ferro.connect(db.url, name=name)
        try:
            live, facts = await _core._live_schema_ir(
                name, json.dumps(sorted(tables_of(parent) | tables_of(child)))
            )
        finally:
            await _core._disconnect(name)
        plan = json.loads(
            _core._plan_from_ir(
                live,
                json.dumps(parent),
                db.backend,
                '{"destructive": true}',
                facts_json=facts,
            )
        )
        return plan["operations"]

    return asyncio.run(read())


def enum_types(db) -> set[str]:
    if db.backend != "postgres":
        return set()
    rows = db.rows(
        "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
        f"WHERE t.typtype = 'e' AND n.nspname = '{db.schema}'"
    )
    return {row[0] for row in rows}


def sql_migration(project: Path, name: str, steps: list[tuple[str, str, str]]) -> Path:
    """The next migration as hand-written portable SQL steps, each
    ``(step name, up, down)``."""
    new(name, "--sql-step", steps[0][0])
    directory = sorted(migrations(project).glob(f"*_{name}"))[-1]
    for ordinal, (step, up, down) in enumerate(steps, start=1):
        (directory / f"{ordinal:02}_{step}.up.sql").write_text(up)
        (directory / f"{ordinal:02}_{step}.down.sql").write_text(down)
    return directory


def table_step(table: str) -> tuple[str, str, str]:
    return (
        table,
        f'CREATE TABLE "{table}" ("id" integer);\n',
        f'DROP TABLE "{table}";\n',
    )


def three_migrations(project, pkg, db) -> None:
    """``0001`` creates ``author`` (generated), ``0002`` and ``0003`` one
    table each (hand-written); all applied."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_migration(project, "second", [table_step("second")])
    sql_migration(project, "third", [table_step("third")])
    assert run("migrate", "up", "--url", db.url) == 0


class Stdin:
    """A stand-in ``sys.stdin`` that says whether it is a terminal."""

    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def answer(monkeypatch, reply: str) -> list[str]:
    """A terminal whose operator answers ``reply``; returns the prompts."""
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return reply

    monkeypatch.setattr(sys, "stdin", Stdin(tty=True))
    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


# -- generated downs reach the parent snapshot ---------------------------------------


def test_down_reverts_a_new_model_and_the_type_it_introduced(project, pkg, db, capsys):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, LIBRARY)
    new("create_post")
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()

    assert run("migrate", "down", "--yes", "--url", db.url) == 0

    out = capsys.readouterr().out
    assert "0002_create_post  01_schema" in out
    assert out.splitlines()[-1].startswith("0002_create_post  01_schema  reverted (")
    assert keys(db) == [(1, 1)]
    assert "post" not in db.tables()
    if db.backend == "postgres":
        assert enum_types(db) == {"status"}
    assert plan_against(db, snapshot(project, 1), snapshot(project, 2)) == []

    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert keys(db) == []
    assert "author" not in db.tables()
    assert enum_types(db) == set()
    assert plan_against(db, empty_snapshot(project), snapshot(project, 1)) == []

    # up resumes from wherever a down stopped.
    assert run("migrate", "up", "--url", db.url) == 0
    assert keys(db) == [(1, 1), (2, 1)]


def test_down_recreates_a_dropped_model_empty_from_the_parent_snapshot(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, LIBRARY)
    new("library")
    write_models(project, pkg, TAG)
    new("drop_library")
    down_file = migration_dir(project, 2) / f"01_schema.down.{db.backend}.sql"
    assert down_file.read_text().startswith("-- ferro: data-dependent\n")
    assert run("migrate", "up", "--url", db.url) == 0
    assert "author" not in db.tables()

    assert run("migrate", "down", "--yes", "--url", db.url) == 0

    assert {"author", "post"} <= db.tables() and "tag" not in db.tables()
    assert db.rows("SELECT count(*) FROM author") == [(0,)]
    assert keys(db) == [(1, 1)]
    assert plan_against(db, snapshot(project, 1), snapshot(project, 2)) == []


# -- targets ------------------------------------------------------------------------------


def test_down_to_a_migration_reverts_newest_first_and_a_failed_down_resumes(
    project, pkg, db, capsys
):
    three_migrations(project, pkg, db)
    second_down = migration_dir(project, 2) / "01_second.down.sql"
    second_down.write_text('DROP TABLE "no_such_table";\n')
    capsys.readouterr()

    assert run("migrate", "down", "--to", "0001", "--yes", "--url", db.url) == 1

    captured = capsys.readouterr()
    assert "0003_third   01_third   reverted (" in captured.out
    assert "ferro migrate: 0002_second/01_second.down.sql failed:" in captured.err
    assert "`ferro migrate down` again" in captured.err
    # 0003's down committed with its record's removal; 0002's down rolled
    # back, so its step stays applied and its record unchanged (#532).
    assert "third" not in db.tables() and "second" in db.tables()
    records = db.records()
    assert [(r[0], r[1]) for r in records] == [(1, 1), (2, 1)]
    failed = records[1]
    assert failed[9] is not None, "the step stays applied"
    assert failed[10] is None and failed[11] is None

    assert run("migrate", "status", "--url", db.url) == 3
    assert "0002_second         installed" in capsys.readouterr().out

    second_down.write_text('DROP TABLE "second";\n')
    assert run("migrate", "down", "--to", "0001", "--yes", "--url", db.url) == 0
    assert keys(db) == [(1, 1)]
    assert "second" not in db.tables() and "author" in db.tables()


def test_down_to_a_step_leaves_the_steps_up_to_it_applied(project, pkg, db, capsys):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_migration(project, "second", [table_step("second")])
    sql_migration(
        project,
        "third",
        [table_step("t_one"), table_step("t_two"), table_step("t_three")],
    )
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()

    assert run("migrate", "down", "--to", "0003:02", "--yes", "--url", db.url) == 0

    assert keys(db) == [(1, 1), (2, 1), (3, 1), (3, 2)]
    assert "t_three" not in db.tables() and "t_two" in db.tables()
    capsys.readouterr()
    assert run("migrate", "status", "--url", db.url) == 3
    assert "0003_third          partial, 2 of 3 steps" in capsys.readouterr().out

    # With no target, down reverts the latest migration, a partly applied one
    # included.
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert keys(db) == [(1, 1), (2, 1)]


def test_down_all_leaves_no_records(project, pkg, db, capsys):
    three_migrations(project, pkg, db)

    assert run("migrate", "down", "--all", "--yes", "--url", db.url) == 0

    assert keys(db) == []
    assert not {"author", "second", "third"} & db.tables()
    capsys.readouterr()
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert capsys.readouterr().out == "nothing to revert\n"


def test_a_nothing_to_reverse_down_runs_nothing_and_removes_the_record(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_migration(
        project,
        "seed",
        [
            (
                "seed",
                "INSERT INTO author (name, status) VALUES ('ada', 'draft');\n",
                "-- ferro: nothing-to-reverse the label stays\n",
            )
        ],
    )
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()

    assert run("migrate", "down", "--yes", "--url", db.url) == 0

    assert "0002_seed  01_seed  nothing to reverse: the label stays" in (
        capsys.readouterr().out
    )
    assert keys(db) == [(1, 1)]
    assert db.rows("SELECT name FROM author") == [("ada",)]


# -- refusals before anything is reverted --------------------------------------------


def test_an_irreversible_step_refuses_the_whole_run_quoting_its_reason(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_migration(
        project,
        "purge",
        [
            table_step("p_one"),
            (
                "purge",
                "DELETE FROM author;\n",
                "-- ferro: irreversible dropped rows cannot come back\n",
            ),
            table_step("p_three"),
        ],
    )
    assert run("migrate", "up", "--url", db.url) == 0
    before = db.records()
    capsys.readouterr()

    assert run("migrate", "down", "--yes", "--url", db.url) == 1

    assert capsys.readouterr().err == (
        "ferro migrate: 0002:02 is irreversible: dropped rows cannot come back\n"
        "There is no flag to skip it: to revert past it, write the step's down in "
        "place of the declaration. Nothing was reverted.\n"
    )
    assert db.records() == before
    assert "p_three" in db.tables()


def test_down_refuses_to_go_below_a_baselined_migration(project, pkg, db, capsys):
    three_migrations(project, pkg, db)
    db.execute("UPDATE _ferro_migrations SET origin = 'baseline' WHERE migration = 1")
    capsys.readouterr()

    assert run("migrate", "down", "--all", "--yes", "--url", db.url) == 1

    assert capsys.readouterr().err == (
        "ferro migrate: 0001 was recorded by `baseline` and created nothing here; "
        "`down` can go no lower than 0001. Nothing was reverted.\n"
    )
    assert keys(db) == [(1, 1), (2, 1), (3, 1)]
    assert run("migrate", "down", "--to", "0001", "--yes", "--url", db.url) == 0
    assert keys(db) == [(1, 1)]


def test_a_target_the_directory_lacks_is_a_refusal(project, pkg, db, capsys):
    three_migrations(project, pkg, db)
    capsys.readouterr()

    assert run("migrate", "down", "--to", "0009", "--yes", "--url", db.url) == 1
    assert "--to 0009 names nothing in migrations/" in capsys.readouterr().err
    assert run("migrate", "down", "--to", "nine", "--yes", "--url", db.url) == 1
    assert "0007:02" in capsys.readouterr().err
    assert len(db.records()) == 3


# -- the prompt -----------------------------------------------------------------------


def test_without_a_terminal_and_without_yes_down_prints_the_plan_and_refuses(
    project, pkg, db, capsys, monkeypatch
):
    three_migrations(project, pkg, db)
    monkeypatch.setattr(sys, "stdin", Stdin(tty=False))
    capsys.readouterr()

    assert run("migrate", "down", "--url", db.url) == 1

    captured = capsys.readouterr()
    assert captured.out == "down reverts, in this order:\n  0003_third  01_third\n"
    assert captured.err == (
        "Not a terminal: pass --yes to revert without a prompt. Nothing was reverted.\n"
    )
    assert len(db.records()) == 3


def test_at_a_terminal_down_asks_and_no_reverts_nothing(
    project, pkg, db, capsys, monkeypatch
):
    three_migrations(project, pkg, db)
    prompts = answer(monkeypatch, "n")
    capsys.readouterr()

    assert run("migrate", "down", "--to", "0001", "--url", db.url) == 0

    assert prompts == ["Revert 0003_third, 0002_second (2 steps)? [y/N] "]
    assert capsys.readouterr().out == (
        "down reverts, in this order:\n"
        "  0003_third   01_third\n"
        "  0002_second  01_second\n"
        "Nothing was reverted.\n"
    )
    assert len(db.records()) == 3

    prompts = answer(monkeypatch, "y")
    assert run("migrate", "down", "--url", db.url) == 0
    assert prompts == ["Revert 0003_third (1 step)? [y/N] "]
    assert keys(db) == [(1, 1), (2, 1)]
