# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""What ``ferro migrate new`` and ``ferro migrate check`` print, in both forms (#651).

```text
$ ferro migrate new add_team --data-step Author
default · migrations
✔ Wrote migrations/0002_add_team/
  01_expand.up.postgres.sql      schema step, up (postgres)
  ...
  02_backfill_author.py          backfill
  04_backfill_author.py          data step
  ir.json                        snapshot

  1 new model
    Team
```

Rich's own detection picks the form. Pytest's capture is not a terminal, so
a test with no environment set pins the **plain form**: ASCII glyphs, no
colour, one name per line, the one-liners and every stderr line byte for
byte what they were before. ``FORCE_COLOR=1`` and ``COLUMNS=100`` give the
**TTY form**, pinned here with the escape sequences Rich writes.
"""

from __future__ import annotations

import sys

import pytest

from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    DIRECTORY_LINE,
    LIBRARY,
    pkg,
    project,
    run,
    write_config,
    write_models,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode", "plain_env"
)

ESC = "\x1b["


@pytest.fixture
def no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write no ``.pyc``: a rewritten ``models.py`` of the same size inside
    the same second would otherwise import the stale one."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


@pytest.fixture
def plain_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """None of the variables Rich reads, whatever the shell running pytest
    set: the plain form unless a test asks for the TTY one."""
    for name in (
        "FORCE_COLOR",
        "NO_COLOR",
        "TTY_COMPATIBLE",
        "TTY_INTERACTIVE",
        "COLUMNS",
        "LINES",
        "COLORTERM",
        "JUPYTER_COLUMNS",
        "JUPYTER_LINES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The TTY form at a 100-column terminal."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "100")


def cli(capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = run("migrate", *argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# -- the models -------------------------------------------------------------------


def many_models(count: int, *, enums: int = 0, rls: bool = False) -> str:
    """``count`` models ``Item00``..., the first ``enums`` of them each with
    an enum of its own (``kind00``...); with ``rls``, ``Item00`` declares row
    security, which SQLite gets a warning for."""
    body = []
    for i in range(count):
        if i < enums:
            body.append(f'\nclass Kind{i:02d}(StrEnum):\n    A = "a"\n')
        lines = [f"\nclass Item{i:02d}(Model):"]
        if rls and i == 0:
            lines.append(
                '    __ferro_rls__ = RowSecurity(RowPolicy(column="owner_id", '
                'setting="app.owner_id"))'
            )
        lines.append(
            "    id: Annotated[int | None, FerroField(primary_key=True)] = None"
        )
        lines.append("    owner_id: int = 0")
        if i < enums:
            lines.append(f"    kind: Kind{i:02d} = Kind{i:02d}.A")
        body.append("\n".join(lines) + "\n")
    return "".join(body)


TEAM = """
class Team(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
"""

AUTHOR_RETYPED = AUTHOR.replace(
    "    name: Annotated[str, FerroField(unique=True)]\n", "    full_name: str\n"
)
"""``name`` dropped and a required ``full_name`` added: an expand, a generated
backfill and a contract."""


RLS_WARNING = (
    "Table 'item00' declares __ferro_rls__, but row-level security is a "
    "PostgreSQL-only feature: the table is created without its policies and rows "
    "are NOT filtered on SQLite. Run against PostgreSQL for enforcement."
)
"""The warning the 45-model project's SQLite rendering carries, longer than
any terminal, so the plain form is seen never to wrap it."""


def first_migration(project, pkg) -> None:
    write_config(project, pkg)
    write_models(project, pkg, many_models(45, enums=24, rls=True))


def add_team(project, pkg) -> None:
    write_config(project, pkg, '["postgres"]')
    write_models(project, pkg, LIBRARY)
    assert run("migrate", "new", "initial") == 0
    write_models(project, pkg, AUTHOR_RETYPED + TEAM)


def replaced_in_place(project, pkg) -> None:
    write_config(project, pkg, '["postgres"]')
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "initial") == 0
    write_models(project, pkg, AUTHOR + "    bio: str | None = None\n")
    assert run("migrate", "new", "add_bio") == 0
    write_models(project, pkg, AUTHOR + "    bio: str | None = None\n" + TEAM)


def two_problems(project, pkg) -> None:
    """An ungenerated change and a data step still holding ``todo(...)``."""
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "initial") == 0
    write_models(project, pkg, AUTHOR + "    slug: str\n")
    assert run("migrate", "new", "add_slug") == 0
    write_models(project, pkg, AUTHOR + "    slug: str\n" + TEAM)


# -- the plain form ---------------------------------------------------------------

SCHEMA_FILES_BOTH = (
    "  01_schema.up.postgres.sql    schema step, up (postgres)\n"
    "  01_schema.down.postgres.sql  schema step, down (postgres)\n"
    "  01_schema.up.sqlite.sql      schema step, up (sqlite)\n"
    "  01_schema.down.sqlite.sql    schema step, down (sqlite)\n"
    "  ir.json                      snapshot\n"
)


def test_plain_first_migration_lists_every_name_on_its_own_line(project, pkg, capsys):
    first_migration(project, pkg)

    code, out, err = cli(capsys, "new", "initial")

    assert code == 0
    models = "".join(f"    Item{i:02d}\n" for i in range(45))
    enums = "".join(f"    kind{i:02d}\n" for i in range(24))
    assert out == (
        f"{DIRECTORY_LINE}\n"
        "ok Wrote migrations/0001_initial/\n"
        f"{SCHEMA_FILES_BOTH}"
        "\n"
        f"  45 new models\n{models}"
        f"  24 new enum types\n{enums}"
    )
    assert err == f"warning: {RLS_WARNING}\n"
    assert ESC not in out + err


def test_plain_add_team_with_a_data_step(project, pkg, capsys):
    add_team(project, pkg)

    code, out, err = cli(capsys, "new", "add_team", "--data-step", "Author")

    assert code == 0
    assert out == (
        f"{DIRECTORY_LINE}\n"
        "ok Wrote migrations/0002_add_team/\n"
        "  01_expand.up.postgres.sql            schema step, up (postgres)\n"
        "  01_expand.down.postgres.sql          schema step, down (postgres)\n"
        "  02_backfill_author.py                backfill\n"
        "  03_add_constraint.up.postgres.sql    schema step, up (postgres)\n"
        "  03_add_constraint.down.postgres.sql  schema step, down (postgres)\n"
        "  04_backfill_author.py                data step\n"
        "  05_contract.up.postgres.sql          schema step, up (postgres)\n"
        "  05_contract.down.postgres.sql        schema step, down (postgres)\n"
        "  ir.json                              snapshot\n"
        "\n"
        "  1 new model\n"
        "    Team\n"
        "  1 changed model\n"
        "    Author\n"
        "  1 dropped model\n"
        "    Post\n"
        "  1 dropped enum type\n"
        "    kind\n"
        '  - author: if "name" became "full_name", declare renamed_from="name"\n'
        "  - 02_backfill_author.py needs writing where it says todo(...); if no "
        "author needs a value, delete 0002_add_team/ and run: ferro migrate new "
        "add_team --no-backfill author.full_name\n"
    )
    assert err == ""


def test_plain_replace_keeps_each_files_replace_word(project, pkg, capsys):
    replaced_in_place(project, pkg)

    code, out, err = cli(capsys, "new", "add_bio", "--replace", "0002_add_bio")

    assert code == 0
    assert out == (
        f"{DIRECTORY_LINE}\n"
        "ok Wrote migrations/0002_add_bio/  (regenerated in place)\n"
        "  01_schema.up.postgres.sql    schema step, up (postgres)    kept\n"
        "  01_schema.down.postgres.sql  schema step, down (postgres)  kept\n"
        "  ir.json                      snapshot                      "
        "new snapshot, chained from 0001_initial\n"
        "\n"
        "  1 new model\n"
        "    Team\n"
        "  1 changed model\n"
        "    Author\n"
    )
    assert err == ""


def test_plain_no_schema_change_is_the_one_liner(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "initial") == 0

    code, out, err = cli(capsys, "new", "again")

    assert (code, out, err) == (
        0,
        f"{DIRECTORY_LINE}\nno schema change: nothing written\n",
        "",
    )


def test_plain_check_ok_is_the_one_liner(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "initial") == 0

    code, out, err = cli(capsys, "check")

    assert (code, out, err) == (
        0,
        f"{DIRECTORY_LINE}\nok: models match 0001_initial\n",
        "",
    )


CHECK_PROBLEMS = (
    "ungenerated: the models changed since 0002_add_slug and no migration records "
    "it (new models: Team); run `ferro migrate new <name>` and commit the "
    "migration it writes\n"
    "unwritten_step: 0002_add_slug/02_backfill_author.py:15: not written yet: the "
    "slug for an existing author\n"
)


def test_plain_check_problems_are_todays_stderr_lines(project, pkg, capsys):
    two_problems(project, pkg)

    code, out, err = cli(capsys, "check")

    assert code == 3
    assert out == f"{DIRECTORY_LINE}\n"
    assert err == CHECK_PROBLEMS


def test_a_refusal_is_the_unchanged_sentence_after_the_database_line(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(
        project,
        pkg,
        "\nclass Keyed(Model):\n"
        "    id: Annotated[int | None, FerroField(primary_key=True)] = None\n"
        "    name: str\n",
    )
    assert run("migrate", "new", "initial") == 0
    write_models(
        project,
        pkg,
        "\nclass Keyed(Model):\n"
        "    id: int | None = None\n"
        "    name: Annotated[str, FerroField(primary_key=True)]\n",
    )

    code, out, err = cli(capsys, "new", "rekey")

    assert code == 1
    assert out == f"{DIRECTORY_LINE}\n"
    assert err.startswith('changing the primary key of "keyed" is not generated'), err
    assert ESC not in err


def test_verbose_changes_nothing_in_a_pipe(project, pkg, capsys):
    first_migration(project, pkg)
    _, quiet, quiet_err = cli(capsys, "new", "initial")
    (project / "migrations/0001_initial").rename(project / "aside")

    code, loud, loud_err = cli(capsys, "new", "initial", "--verbose")

    assert code == 0
    assert (loud, loud_err) == (quiet, quiet_err)
    _, check_quiet, _ = cli(capsys, "check")
    _, check_loud, _ = cli(capsys, "check", "--verbose")
    assert check_loud == check_quiet


def test_colour_never_carries_meaning_alone(project, pkg, capsys, monkeypatch):
    """Every file line has its role words, every name list its count
    heading, read on the text with the escapes stripped."""
    import re

    add_team(project, pkg)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "100")

    code, out, _ = cli(capsys, "new", "add_team", "--data-step", "Author")

    assert code == 0
    text = re.sub(r"\x1b\[[0-9;]*m", "", out)
    lines = text.splitlines()
    files = lines[2 : lines.index("")]
    roles = (
        "schema step, up",
        "schema step, down",
        "backfill",
        "data step",
        "snapshot",
    )
    assert files and all(any(r in line for r in roles) for line in files), text
    for heading in (
        "1 new model",
        "1 changed model",
        "1 dropped model",
        "1 dropped enum type",
    ):
        assert f"  {heading}" in lines, text


# -- the TTY form -----------------------------------------------------------------
#
# Pinned from what the command printed, escapes and all; a change to the look
# shows here as a diff of these strings.

TTY_FIRST_OUT = (
    "\x1b[1mdefault\x1b[0m · \x1b[2mmigrations\x1b[0m\n"
    "\x1b[1;32m✔\x1b[0m \x1b[1mWrote migrations/0001_initial/\x1b[0m\n"
    "  \x1b[36m01_schema\x1b[0m\x1b[2m.up.postgres.sql\x1b[0m    \x1b[2m"
    "schema step, up (postgres)\x1b[0m\n"
    "  \x1b[2;36m01_schema\x1b[0m\x1b[2m.down.postgres.sql\x1b[0m  \x1b[2m"
    "schema step, down (postgres)\x1b[0m\n"
    "  \x1b[36m01_schema\x1b[0m\x1b[2m.up.sqlite.sql\x1b[0m      \x1b[2m"
    "schema step, up (sqlite)\x1b[0m\n"
    "  \x1b[2;36m01_schema\x1b[0m\x1b[2m.down.sqlite.sql\x1b[0m    \x1b[2m"
    "schema step, down (sqlite)\x1b[0m\n"
    "  \x1b[2mir\x1b[0m\x1b[2m.json\x1b[0m                      \x1b[2m"
    "snapshot\x1b[0m\n"
    "\n"
    "\x1b[1m  45 new models\x1b[0m\n"
    "    \x1b[32mItem00\x1b[0m  \x1b[32mItem01\x1b[0m  \x1b[32mItem02\x1b[0m"
    "  \x1b[32mItem03\x1b[0m  \x1b[32mItem04\x1b[0m  \x1b[32mItem05\x1b[0m\n"
    "\x1b[2m    … and 39 more (--verbose shows them all)\x1b[0m\n"
    "\x1b[1m  24 new enum types\x1b[0m\n"
    "    \x1b[32mkind00\x1b[0m  \x1b[32mkind01\x1b[0m  \x1b[32mkind02\x1b[0m"
    "  \x1b[32mkind03\x1b[0m  \x1b[32mkind04\x1b[0m  \x1b[32mkind05\x1b[0m\n"
    "\x1b[2m    … and 18 more (--verbose shows them all)\x1b[0m\n"
)
TTY_FIRST_ERR = (
    "\x1b[1;33m▲\x1b[0m\x1b[1;33m warning\x1b[0m  Table 'item00' declares "
    "__ferro_rls__, but row-level security is a PostgreSQL-only feature: the "
    "table is created without its policies and rows are NOT filtered on "
    "SQLite. Run against PostgreSQL for enforcement.\n"
)

TTY_TEAM_OUT = (
    "\x1b[1mdefault\x1b[0m · \x1b[2mmigrations\x1b[0m\n"
    "\x1b[1;32m✔\x1b[0m \x1b[1mWrote migrations/0002_add_team/\x1b[0m\n"
    "  \x1b[36m01_expand\x1b[0m\x1b[2m.up.postgres.sql\x1b[0m            "
    "\x1b[2mschema step, up (postgres)\x1b[0m\n"
    "  \x1b[2;36m01_expand\x1b[0m\x1b[2m.down.postgres.sql\x1b[0m          "
    "\x1b[2mschema step, down (postgres)\x1b[0m\n"
    "  \x1b[35m02_backfill_author\x1b[0m\x1b[2m.py\x1b[0m                "
    "\x1b[2mbackfill\x1b[0m\n"
    "  \x1b[36m03_add_constraint\x1b[0m\x1b[2m.up.postgres.sql\x1b[0m    "
    "\x1b[2mschema step, up (postgres)\x1b[0m\n"
    "  \x1b[2;36m03_add_constraint\x1b[0m\x1b[2m.down.postgres.sql\x1b[0m  "
    "\x1b[2mschema step, down (postgres)\x1b[0m\n"
    "  \x1b[35m04_backfill_author\x1b[0m\x1b[2m.py\x1b[0m                "
    "\x1b[2mdata step\x1b[0m\n"
    "  \x1b[36m05_contract\x1b[0m\x1b[2m.up.postgres.sql\x1b[0m          "
    "\x1b[2mschema step, up (postgres)\x1b[0m\n"
    "  \x1b[2;36m05_contract\x1b[0m\x1b[2m.down.postgres.sql\x1b[0m        "
    "\x1b[2mschema step, down (postgres)\x1b[0m\n"
    "  \x1b[2mir\x1b[0m\x1b[2m.json\x1b[0m                              "
    "\x1b[2msnapshot\x1b[0m\n"
    "\n"
    "\x1b[1m  1 new model\x1b[0m\n"
    "    \x1b[32mTeam\x1b[0m\n"
    "\x1b[1m  1 changed model\x1b[0m\n"
    "    \x1b[33mAuthor\x1b[0m\n"
    "\x1b[1m  1 dropped model\x1b[0m\n"
    "    \x1b[31mPost\x1b[0m\n"
    "\x1b[1m  1 dropped enum type\x1b[0m\n"
    "    \x1b[31mkind\x1b[0m\n"
    '  \x1b[2m•\x1b[0m author: if "name" became "full_name", declare '
    'renamed_from="name"\n'
    "  \x1b[2m•\x1b[0m 02_backfill_author.py needs writing where it says "
    "todo(...); if no author needs a value, delete 0002_add_team/ and run: "
    "ferro migrate new add_team --no-backfill author.full_name\n"
)

TTY_REPLACE_OUT = (
    "\x1b[1mdefault\x1b[0m · \x1b[2mmigrations\x1b[0m\n"
    "\x1b[1;32m✔\x1b[0m \x1b[1mWrote migrations/0002_add_bio/\x1b[0m\x1b[2m  "
    "(regenerated in place)\x1b[0m\n"
    "  \x1b[36m01_schema\x1b[0m\x1b[2m.up.postgres.sql\x1b[0m    \x1b[2m"
    "schema step, up (postgres)  \x1b[0m  kept\n"
    "  \x1b[2;36m01_schema\x1b[0m\x1b[2m.down.postgres.sql\x1b[0m  \x1b[2m"
    "schema step, down (postgres)\x1b[0m  kept\n"
    "  \x1b[2mir\x1b[0m\x1b[2m.json\x1b[0m                      \x1b[2m"
    "snapshot                    \x1b[0m  new snapshot, chained from "
    "0001_initial\n"
    "\n"
    "\x1b[1m  1 new model\x1b[0m\n"
    "    \x1b[32mTeam\x1b[0m\n"
    "\x1b[1m  1 changed model\x1b[0m\n"
    "    \x1b[33mAuthor\x1b[0m\n"
)

TTY_CHECKOK_OUT = (
    "\x1b[1mdefault\x1b[0m · \x1b[2mmigrations\x1b[0m\n"
    "\x1b[1;32m✔\x1b[0m \x1b[1mmodels match\x1b[0m\x1b[2m  0001_initial"
    "\x1b[0m\n"
)

TTY_PROBLEMS_OUT = "\x1b[1mdefault\x1b[0m · \x1b[2mmigrations\x1b[0m\n"
TTY_PROBLEMS_ERR = (
    "\x1b[1;31m✘\x1b[0m \x1b[1;31m2 problems since 0002_add_slug\x1b[0m\n"
    "ungenerated: the models changed since 0002_add_slug and no migration "
    "records it (new models: Team); run `ferro migrate new <name>` and "
    "commit the migration it writes\n"
    "unwritten_step: 0002_add_slug/02_backfill_author.py:15: not written "
    "yet: the slug for an existing author\n"
)

TTY_NOCHANGE_OUT = (
    "\x1b[1mdefault\x1b[0m · \x1b[2mmigrations\x1b[0m\n"
    "no schema change: nothing written\n"
)

TTY_TWELVE_OUT = (
    "\x1b[1mdefault\x1b[0m · \x1b[2mmigrations\x1b[0m\n"
    "\x1b[1;32m✔\x1b[0m \x1b[1mWrote migrations/0001_initial/\x1b[0m\n"
    "  \x1b[36m01_schema\x1b[0m\x1b[2m.up.postgres.sql\x1b[0m    \x1b[2m"
    "schema step, up (postgres)\x1b[0m\n"
    "  \x1b[2;36m01_schema\x1b[0m\x1b[2m.down.postgres.sql\x1b[0m  \x1b[2m"
    "schema step, down (postgres)\x1b[0m\n"
    "  \x1b[2mir\x1b[0m\x1b[2m.json\x1b[0m                      \x1b[2m"
    "snapshot\x1b[0m\n"
    "\n"
    "\x1b[1m  12 new models\x1b[0m\n"
    "    \x1b[32mItem00\x1b[0m  \x1b[32mItem01\x1b[0m  \x1b[32mItem02\x1b[0m"
    "  \x1b[32mItem03\x1b[0m  \x1b[32mItem04\x1b[0m  \x1b[32mItem05\x1b[0m  "
    "\x1b[32mItem06\x1b[0m  \x1b[32mItem07\x1b[0m  \x1b[32mItem08\x1b[0m  "
    "\x1b[32mItem09\x1b[0m  \x1b[32mItem10\x1b[0m  \x1b[32mItem11\x1b[0m\n"
)

TTY_THIRTEEN_OUT = (
    "\x1b[1mdefault\x1b[0m · \x1b[2mmigrations\x1b[0m\n"
    "\x1b[1;32m✔\x1b[0m \x1b[1mWrote migrations/0001_initial/\x1b[0m\n"
    "  \x1b[36m01_schema\x1b[0m\x1b[2m.up.postgres.sql\x1b[0m    \x1b[2m"
    "schema step, up (postgres)\x1b[0m\n"
    "  \x1b[2;36m01_schema\x1b[0m\x1b[2m.down.postgres.sql\x1b[0m  \x1b[2m"
    "schema step, down (postgres)\x1b[0m\n"
    "  \x1b[2mir\x1b[0m\x1b[2m.json\x1b[0m                      \x1b[2m"
    "snapshot\x1b[0m\n"
    "\n"
    "\x1b[1m  13 new models\x1b[0m\n"
    "    \x1b[32mItem00\x1b[0m  \x1b[32mItem01\x1b[0m  \x1b[32mItem02\x1b[0m"
    "  \x1b[32mItem03\x1b[0m  \x1b[32mItem04\x1b[0m  \x1b[32mItem05\x1b[0m\n"
    "\x1b[2m    … and 7 more (--verbose shows them all)\x1b[0m\n"
)

TTY_THIRTEEN_VERBOSE_OUT = (
    "\x1b[1mdefault\x1b[0m · \x1b[2mmigrations\x1b[0m\n"
    "\x1b[1;32m✔\x1b[0m \x1b[1mWrote migrations/0001_initial/\x1b[0m\n"
    "  \x1b[36m01_schema\x1b[0m\x1b[2m.up.postgres.sql\x1b[0m    \x1b[2m"
    "schema step, up (postgres)\x1b[0m\n"
    "  \x1b[2;36m01_schema\x1b[0m\x1b[2m.down.postgres.sql\x1b[0m  \x1b[2m"
    "schema step, down (postgres)\x1b[0m\n"
    "  \x1b[2mir\x1b[0m\x1b[2m.json\x1b[0m                      \x1b[2m"
    "snapshot\x1b[0m\n"
    "\n"
    "\x1b[1m  13 new models\x1b[0m\n"
    "    \x1b[32mItem00\x1b[0m  \x1b[32mItem02\x1b[0m  \x1b[32mItem04\x1b[0m"
    "  \x1b[32mItem06\x1b[0m  \x1b[32mItem08\x1b[0m  \x1b[32mItem10\x1b[0m  "
    "\x1b[32mItem12\x1b[0m\n"
    "    \x1b[32mItem01\x1b[0m  \x1b[32mItem03\x1b[0m  \x1b[32mItem05\x1b[0m"
    "  \x1b[32mItem07\x1b[0m  \x1b[32mItem09\x1b[0m  \x1b[32mItem11\x1b[0m\n"
)


def test_tty_first_migration_collapses_its_long_lists(project, pkg, capsys, tty):
    first_migration(project, pkg)

    assert cli(capsys, "new", "initial") == (0, TTY_FIRST_OUT, TTY_FIRST_ERR)


def test_tty_add_team_with_a_data_step(project, pkg, capsys, tty):
    add_team(project, pkg)

    assert cli(capsys, "new", "add_team", "--data-step", "Author") == (
        0,
        TTY_TEAM_OUT,
        "",
    )


def test_tty_replace_keeps_each_files_replace_word(project, pkg, capsys, tty):
    replaced_in_place(project, pkg)

    assert cli(capsys, "new", "add_bio", "--replace", "0002_add_bio") == (
        0,
        TTY_REPLACE_OUT,
        "",
    )


def test_tty_check_ok_and_no_schema_change(project, pkg, capsys, tty):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "initial") == 0

    assert cli(capsys, "check") == (0, TTY_CHECKOK_OUT, "")
    assert cli(capsys, "new", "again") == (0, TTY_NOCHANGE_OUT, "")


def test_tty_check_problems_sit_under_a_red_heading(project, pkg, capsys, tty):
    two_problems(project, pkg)

    assert cli(capsys, "check") == (3, TTY_PROBLEMS_OUT, TTY_PROBLEMS_ERR)


@pytest.mark.parametrize(
    ("count", "flags", "expected"),
    [
        pytest.param(12, (), "TTY_TWELVE_OUT", id="12-whole"),
        pytest.param(13, (), "TTY_THIRTEEN_OUT", id="13-collapsed"),
        pytest.param(13, ("--verbose",), "TTY_THIRTEEN_VERBOSE_OUT", id="13-verbose"),
    ],
)
def test_tty_a_list_longer_than_twelve_collapses_unless_verbose(
    project, pkg, capsys, tty, count, flags, expected
):
    write_config(project, pkg, '["postgres"]')
    write_models(project, pkg, many_models(count))

    assert cli(capsys, "new", "initial", *flags) == (0, globals()[expected], "")


def test_no_color_keeps_the_tty_layout_without_an_escape(
    project, pkg, capsys, tty, monkeypatch
):
    import re

    monkeypatch.setenv("NO_COLOR", "1")
    first_migration(project, pkg)

    code, out, err = cli(capsys, "new", "initial")
    _, check_out, _ = cli(capsys, "check")

    assert code == 0
    assert ESC not in out + err + check_out
    strip = re.compile(r"\x1b\[[0-9;]*m")
    assert out == strip.sub("", TTY_FIRST_OUT)
    assert err == strip.sub("", TTY_FIRST_ERR)
    assert check_out.splitlines()[1] == "✔ models match  0001_initial"


def test_a_dumb_terminal_gets_the_ascii_glyphs(project, pkg, capsys, tty, monkeypatch):
    import re

    monkeypatch.setenv("TERM", "dumb")
    write_config(project, pkg, '["postgres"]')
    write_models(project, pkg, many_models(13))

    code, out, _ = cli(capsys, "new", "initial")

    lines = re.sub(r"\x1b\[[0-9;]*m", "", out).splitlines()
    assert code == 0
    assert lines[1] == "ok Wrote migrations/0001_initial/"
    assert lines[-1] == "    ... and 7 more (--verbose shows them all)"


# -- a width nobody can use ---------------------------------------------------------


@pytest.mark.parametrize("columns", ["0", "abc"])
def test_plain_check_prints_every_line_whatever_columns_says(
    project, pkg, capsys, monkeypatch, columns
):
    """The plain form never asks the width whether to print a line: a pipe
    under ``COLUMNS=0`` prints the same bytes as without it."""
    monkeypatch.setenv("COLUMNS", columns)
    two_problems(project, pkg)

    assert cli(capsys, "check") == (3, f"{DIRECTORY_LINE}\n", CHECK_PROBLEMS)


@pytest.mark.parametrize("columns", ["0", "abc"])
def test_tty_new_and_check_print_every_line_whatever_columns_says(
    project, pkg, capsys, monkeypatch, columns
):
    """On a terminal a width below one is no width: the layout falls back to
    eighty columns, the width Rich assumes when it cannot tell."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", columns)
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)

    code, out, err = cli(capsys, "new", "initial")

    assert (code, err) == (0, "")
    assert "Wrote migrations/0001_initial/" in out
    assert "1 new model" in out
    assert cli(capsys, "check") == (0, TTY_CHECKOK_OUT, "")


# -- a refusal a verb raises ------------------------------------------------------


def rekeyed(project, pkg) -> None:
    """A primary key moved to another column: ``new`` refuses with the
    primary-key recipe."""
    write_config(project, pkg)
    write_models(
        project,
        pkg,
        "\nclass Keyed(Model):\n"
        "    id: Annotated[int | None, FerroField(primary_key=True)] = None\n"
        "    name: str\n",
    )
    assert run("migrate", "new", "initial") == 0
    write_models(
        project,
        pkg,
        "\nclass Keyed(Model):\n"
        "    id: int | None = None\n"
        "    name: Annotated[str, FerroField(primary_key=True)]\n",
    )


def refused_heading(verb: str) -> str:
    return f"\x1b[1;31m✘\x1b[0m \x1b[1;31m{verb} refused\x1b[0m\n"


def test_tty_new_refusal_sits_under_a_red_heading_naming_the_verb(
    project, pkg, capsys, monkeypatch
):
    rekeyed(project, pkg)
    code, _, plain_err = cli(capsys, "new", "rekey")
    assert code == 1
    assert plain_err.startswith('changing the primary key of "keyed"'), plain_err
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "100")

    code, out, err = cli(capsys, "new", "rekey")

    assert code == 1
    assert out == "\x1b[1mdefault\x1b[0m · \x1b[2mmigrations\x1b[0m\n"
    assert err == refused_heading("new") + plain_err


def test_a_settings_refusal_in_both_forms(project, capsys, monkeypatch):
    (project / "ferro.toml").write_text("models = [\n")
    code, out, plain_err = cli(capsys, "check")
    assert (code, out) == (1, "")
    assert plain_err.endswith(
        "/ferro.toml is not valid TOML: Invalid value (at end of document); "
        "fix the file\n"
    ), plain_err
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "100")

    assert cli(capsys, "check") == (1, "", refused_heading("check") + plain_err)


def test_a_refusal_with_no_verb_is_headed_ferro_refused(capsys, monkeypatch):
    from ferro.cli import exit_codes, render_refusal
    from ferro.settings import SettingsError

    monkeypatch.setenv("FORCE_COLOR", "1")

    assert render_refusal(SettingsError("bad")) == exit_codes.REFUSED
    assert capsys.readouterr().err == refused_heading("ferro") + "bad\n"
