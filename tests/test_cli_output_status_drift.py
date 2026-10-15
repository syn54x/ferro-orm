# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""What ``ferro migrate status`` and ``ferro migrate drift`` print, in both forms (#652).

```text
$ ferro migrate status
default · sqlite:app.db
!! 1 failed · 1 pending  main._ferro_migrations
  ~~ 0001_create_author  partial, 1 of 2 steps  2026-10-14 09:40Z
      01_schema.up.sqlite.sql  applied
      02_two_b.up.sql          failed
        no such table: missing

xx needs attention
  0001_create_author/02_two_b.up.sql failed ...
```

Pytest's capture is not a terminal, so a test with no environment set pins
the **plain form**: ASCII glyphs, no colour, every row and every finding on
a line of its own. ``FORCE_COLOR=1`` and ``COLUMNS=100`` give the **TTY
form**, pinned with the escape sequences Rich writes. A time a run records
is read back from ``status --json`` and shown to the minute with ``Z``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ferro.cli.output import applied_at_short, kind_of_file
from tests.test_cli_output_new_check import (  # noqa: F401 - fixtures
    no_bytecode,
    plain_env,
    tty,
)
from tests.test_migrate_baseline import auto_migrated
from tests.test_migrate_drift import drift_api
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
    migrations,
    new,
    sha384,
    sql_step,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode", "plain_env"
)

ESC = "\x1b["


def cli(capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = run("migrate", *argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def table(db) -> str:
    schema = "main" if db.backend == "sqlite" else db.schema
    return f"{schema}._ferro_migrations"


def document(capsys, db) -> dict:
    code, out, _ = cli(capsys, "status", "--json", "--url", db.url)
    return json.loads(out)


def at(document: dict, number: int) -> str:
    """The migration's ``applied_at`` as ``status`` shows it."""
    return applied_at_short(document["migrations"][number - 1]["applied_at"])


# -- the projects -----------------------------------------------------------------


def two_pending(project, pkg, db) -> None:
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_step(project, "second", 'CREATE TABLE "second" ("id" integer);\n')


def two_applied(project, pkg, db) -> None:
    two_pending(project, pkg, db)
    assert run("migrate", "up", "--url", db.url) == 0


def failed_second_step(project, pkg, db) -> None:
    """``0001`` with a second, hand-written step that fails."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    (folder,) = migrations(project).glob("0001_*")
    (folder / "02_two_b.up.sql").write_text('INSERT INTO "missing" VALUES (1);\n')
    (folder / "02_two_b.down.sql").write_text("SELECT 1;\n")
    assert run("migrate", "up", "--url", db.url) == 1


def edited_applied_file(project, pkg, db) -> tuple[str, str, str]:
    """``0001`` applied, then its up file edited: the file's name and the
    applied and on-disk checksums."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    assert run("migrate", "up", "--url", db.url) == 0
    up_file = migrations(project) / f"0001_create_author/01_schema.up.{db.backend}.sql"
    applied = sha384(up_file)
    up_file.write_bytes(up_file.read_bytes() + b"\n")
    return up_file.name, applied, sha384(up_file)


def ahead_of_the_directory(project, pkg, db) -> None:
    two_applied(project, pkg, db)
    shutil.rmtree(migrations(project) / "0002_second")


# -- the plain form: status --------------------------------------------------------


@pytest.mark.backend_matrix
def test_plain_status_before_any_run(project, pkg, db, capsys):
    two_pending(project, pkg, db)

    code, out, err = cli(capsys, "status", "--url", db.url)

    assert code == 3
    assert out == (
        f"{db.line}\n"
        f"-- 2 pending  {table(db)}\n"
        "  -- 0001_create_author  pending\n"
        "  -- 0002_second         pending\n"
    )
    assert err == ""


@pytest.mark.backend_matrix
def test_plain_status_after_up(project, pkg, db, capsys):
    two_applied(project, pkg, db)
    doc = document(capsys, db)

    code, out, _ = cli(capsys, "status", "--url", db.url)

    assert code == 0
    assert out == (
        f"{db.line}\n"
        f"ok 2 applied  {table(db)}\n"
        f"  ok 0001_create_author  applied  {at(doc, 1)}\n"
        f"  ok 0002_second         applied  {at(doc, 2)}\n"
    )
    assert at(doc, 1).endswith("Z") and len(at(doc, 1)) == len("2026-10-14 09:40Z")


@pytest.mark.backend_matrix
def test_plain_status_with_steps_expands_every_migration(project, pkg, db, capsys):
    two_applied(project, pkg, db)
    doc = document(capsys, db)

    code, out, _ = cli(capsys, "status", "--steps", "--url", db.url)

    assert code == 0
    assert out == (
        f"{db.line}\n"
        f"ok 2 applied  {table(db)}\n"
        f"  ok 0001_create_author  applied  {at(doc, 1)}\n"
        f"      01_schema.up.{db.backend}.sql  applied  expand\n"
        f"  ok 0002_second         applied  {at(doc, 2)}\n"
        "      01_second.up.sql  applied  contract\n"
    )


@pytest.mark.backend_matrix
def test_plain_status_json_is_the_whole_of_stdout(project, pkg, db, capsys):
    two_applied(project, pkg, db)

    code, out, _ = cli(capsys, "status", "--steps", "--json", "--url", db.url)

    assert code == 0
    assert out.startswith("{")
    parsed = json.loads(out)
    assert [m["name"] for m in parsed["migrations"]] == [
        "0001_create_author",
        "0002_second",
    ]


@pytest.mark.backend_matrix
def test_plain_status_a_failed_step_needs_attention_on_stdout(
    project, pkg, db, capsys
):
    failed_second_step(project, pkg, db)
    doc = document(capsys, db)
    failed = doc["migrations"][0]["steps"][1]
    # A failed step is retried by the next up: nothing refuses.
    assert failed["error"] and doc["refusal"] is None

    code, out, err = cli(capsys, "status", "--url", db.url)

    assert code == 4
    error = "".join(f"        {line}\n" for line in failed["error"].splitlines())
    up_file = f"01_schema.up.{db.backend}.sql"
    width = len(up_file)
    assert out == (
        f"{db.line}\n"
        f"!! 1 failed · 1 pending  {table(db)}\n"
        f"  ~~ 0001_create_author  partial, 1 of 2 steps  {at(doc, 1)}\n"
        f"      {up_file}  applied\n"
        f"      {'02_two_b.up.sql':<{width}}  failed\n"
        f"{error}"
    )
    assert err == ""


@pytest.mark.backend_matrix
def test_plain_status_an_edited_file_shows_both_checksums(project, pkg, db, capsys):
    name, applied, on_disk = edited_applied_file(project, pkg, db)
    doc = document(capsys, db)

    code, out, err = cli(capsys, "status", "--url", db.url)

    assert code == 4
    head = (
        f"{db.line}\n"
        f"!! 1 edited  {table(db)}\n"
        f"  xx 0001_create_author  applied (different checksum)  {at(doc, 1)}\n"
        f"      {name}  applied (different checksum)\n"
        f"        applied  sha384:{applied}\n"
        f"        on disk  sha384:{on_disk}\n"
    )
    refusal = doc["refusal"]
    assert refusal is not None and "ferro migrate rerecord 0001:01" in refusal
    shown = "".join(f"  {line}\n" for line in refusal.splitlines())
    assert out == f"{head}\nxx needs attention\n{shown}"
    assert err == ""


@pytest.mark.backend_matrix
def test_plain_status_a_database_ahead_of_the_directory(project, pkg, db, capsys):
    ahead_of_the_directory(project, pkg, db)
    doc = document(capsys, db)
    refusal = doc["refusal"]
    assert refusal is not None and "not in migrations/" in refusal

    code, out, err = cli(capsys, "status", "--url", db.url)

    assert code == 4
    shown = "".join(f"  {line}\n" for line in refusal.splitlines())
    assert out == (
        f"{db.line}\n"
        f"!! 1 ahead  {table(db)}\n"
        f"  ok 0001_create_author  applied  {at(doc, 1)}\n"
        "  xx 0002_second         applied, not in the directory\n"
        "\n"
        "xx needs attention\n"
        f"{shown}"
    )
    assert err == ""


def unreadable_directory(project, pkg, db) -> None:
    """Both migrations applied, then an empty ``0003_empty/`` in the
    directory: status cannot read the directory, so it lists nothing and
    only the refusal says why."""
    two_applied(project, pkg, db)
    (migrations(project) / "0003_empty").mkdir()


@pytest.mark.backend_matrix
def test_plain_status_with_only_a_refusal_heads_it_needs_attention(
    project, pkg, db, capsys
):
    unreadable_directory(project, pkg, db)
    doc = document(capsys, db)
    assert doc["migrations"] == [] and doc["ahead"] == []
    refusal = doc["refusal"]
    assert refusal is not None

    code, out, err = cli(capsys, "status", "--url", db.url)

    assert code == 4
    shown = "".join(f"  {line}\n" for line in refusal.splitlines())
    assert out == (
        f"{db.line}\n"
        f"!! needs attention  {table(db)}\n"
        "\n"
        "xx needs attention\n"
        f"{shown}"
    )
    assert err == ""


@pytest.mark.backend_matrix
def test_plain_status_a_baseline(project, pkg, db, capsys):
    auto_migrated(project, pkg, db)
    assert run("migrate", "baseline", "--url", db.url) == 0
    doc = document(capsys, db)
    names = [m["name"] for m in doc["migrations"]]
    width = max(len(n) for n in names)

    code, out, _ = cli(capsys, "status", "--url", db.url)

    assert code == 0
    rows = "".join(
        f"  ok {name:<{width}}  applied (baseline)  {at(doc, i)}\n"
        for i, name in enumerate(names, start=1)
    )
    assert out == f"{db.line}\nok 2 applied  {table(db)}\n{rows}"


# -- the plain form: drift ---------------------------------------------------------


def drifted(project, pkg, db) -> None:
    """``0001`` applied, then two indexes added by hand."""
    two_applied(project, pkg, db)
    db.execute('CREATE INDEX "idx_author_status" ON "author" ("status")')
    db.execute('CREATE INDEX "idx_author_name" ON "author" ("name")')


@pytest.mark.backend_matrix
def test_plain_drift_clean_is_the_one_liner(project, pkg, db, capsys):
    two_applied(project, pkg, db)

    code, out, err = cli(capsys, "drift", "--url", db.url)

    assert (code, out, err) == (0, f"{db.line}\nno drift against 0002_second\n", "")


@pytest.mark.backend_matrix
def test_plain_drift_lists_every_finding_under_its_count(project, pkg, db, capsys):
    drifted(project, pkg, db)

    code, out, err = cli(capsys, "drift", "--url", db.url)

    assert code == 4
    lines = out.splitlines()
    assert lines[:2] == [db.line, "xx 2 differences against 0002_second"]
    assert sorted(lines[2:]) == [
        "  - idx_author_name index is extra",
        "  - idx_author_status index is extra",
    ]
    assert err == ""


@pytest.mark.backend_matrix
def test_plain_drift_refusal_is_the_sentence_on_stderr(project, pkg, db, capsys):
    two_applied(project, pkg, db)
    db.execute("UPDATE _ferro_migrations SET finished_at = NULL WHERE migration = 2")

    code, out, err = cli(capsys, "drift", "--url", db.url)

    assert (code, out) == (4, f"{db.line}\n")
    assert err == f"{drift_api(db).refusal}\n"


LEDGER = (
    AUTHOR
    + """

class Ledger(Model):
    __ferro_rls__ = RowSecurity(RowPolicy(column="owner_id", setting="app.owner_id"))

    id: Annotated[int | None, FerroField(primary_key=True)] = None
    owner_id: int
"""
)

FOREIGN_POLICY = (
    "Table 'ledger' carries row policy/policies 'hand_made' that ferro does not "
    "own (their names do not start with 'rls_'). They still filter rows and "
    "compose with the declared policies. Ferro never alters or drops them."
)


def foreign_policy(project, pkg, db) -> None:
    """A row-secured table applied, then a policy ferro does not own added by
    hand: drift warns about it and finds no difference."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, LEDGER)
    new("ledger")
    assert run("migrate", "up", "--url", db.url) == 0
    db.execute('CREATE POLICY "hand_made" ON "ledger" USING (true)')


@pytest.mark.postgres_only
def test_plain_drift_warnings_go_to_stderr(project, pkg, db, capsys):
    foreign_policy(project, pkg, db)

    code, out, err = cli(capsys, "drift", "--url", db.url)

    assert (code, out) == (0, f"{db.line}\nno drift against 0001_ledger\n")
    assert err == f"warning: {FOREIGN_POLICY}\n"


# -- the TTY form ------------------------------------------------------------------


def sgr(codes: str, text: str) -> str:
    return f"{ESC}{codes}m{text}{ESC}0m"


def tty_line(db) -> str:
    """The database line on a terminal: the name bold, the URL dim."""
    name, where = db.line.split(" · ", 1)
    return f"{sgr('1', name)} · {sgr('2', where)}"


def tty_heading(glyph: str, codes: str, title: str, where: str) -> str:
    return f"{sgr(codes, glyph)} {sgr('1', title)}{sgr('2', f'  {where}')}\n"


@pytest.mark.backend_matrix
def test_tty_status_before_any_run(project, pkg, db, capsys, tty):
    two_pending(project, pkg, db)

    code, out, err = cli(capsys, "status", "--url", db.url)

    assert code == 3
    pending = sgr("1;33", "○")
    assert out == (
        f"{tty_line(db)}\n"
        + tty_heading("○", "1;33", "2 pending", table(db))
        + f"  {pending} 0001_create_author  {sgr('33', 'pending')}\n"
        f"  {pending} 0002_second         {sgr('33', 'pending')}\n"
    )
    assert err == ""


@pytest.mark.backend_matrix
def test_tty_status_after_up_with_steps(project, pkg, db, capsys, tty):
    two_applied(project, pkg, db)
    doc = document(capsys, db)

    code, out, _ = cli(capsys, "status", "--url", db.url)

    assert code == 0
    ok = sgr("1;32", "✔")
    applied = sgr("32", "applied")
    first = f"  {ok} 0001_create_author  {applied}{sgr('2', f'  {at(doc, 1)}')}\n"
    second = f"  {ok} 0002_second         {applied}{sgr('2', f'  {at(doc, 2)}')}\n"
    head = f"{tty_line(db)}\n" + tty_heading("✔", "1;32", "2 applied", table(db))
    assert out == head + first + second

    code, out, _ = cli(capsys, "status", "--steps", "--url", db.url)

    assert code == 0
    assert out == (
        head
        + first
        + f"      {sgr('36', '01_schema')}{sgr('2', f'.up.{db.backend}.sql')}"
        f"  {applied}{sgr('2', '  expand')}\n"
        + second
        + f"      {sgr('34', '01_second')}{sgr('2', '.up.sql')}"
        f"  {applied}{sgr('2', '  contract')}\n"
    )


@pytest.mark.backend_matrix
def test_tty_status_a_failed_step(project, pkg, db, capsys, tty):
    failed_second_step(project, pkg, db)
    doc = document(capsys, db)
    error = doc["migrations"][0]["steps"][1]["error"]

    code, out, err = cli(capsys, "status", "--url", db.url)

    assert code == 4
    up_file = f"01_schema.up.{db.backend}.sql"
    pad = " " * (len(up_file) - len("02_two_b.up.sql") + 2)
    assert out == (
        f"{tty_line(db)}\n"
        + tty_heading("▲", "1;33", "1 failed · 1 pending", table(db))
        + f"  {sgr('1;33', '◐')} 0001_create_author  "
        f"{sgr('33', 'partial, 1 of 2 steps')}{sgr('2', f'  {at(doc, 1)}')}\n"
        f"      {sgr('36', '01_schema')}{sgr('2', f'.up.{db.backend}.sql')}"
        f"  {sgr('32', 'applied')}\n"
        f"      {sgr('34', '02_two_b')}{sgr('2', '.up.sql')}{pad}"
        f"{sgr('1;31', 'failed')}\n"
        + "".join(f"{sgr('31', f'        {line}')}\n" for line in error.splitlines())
    )
    assert err == ""


@pytest.mark.backend_matrix
def test_tty_status_an_edited_file(project, pkg, db, capsys, tty):
    name, applied, on_disk = edited_applied_file(project, pkg, db)
    doc = document(capsys, db)

    code, out, _ = cli(capsys, "status", "--url", db.url)

    assert code == 4
    edited = "applied (different checksum)"
    assert out == (
        f"{tty_line(db)}\n"
        + tty_heading("▲", "1;33", "1 edited", table(db))
        + f"  {sgr('1;31', '✘')} 0001_create_author  "
        f"{sgr('31', edited)}{sgr('2', f'  {at(doc, 1)}')}\n"
        f"      {sgr('36', '01_schema')}{sgr('2', f'.up.{db.backend}.sql')}"
        f"  {sgr('31', edited)}\n"
        f"{sgr('2', f'        applied  sha384:{applied}')}\n"
        f"{sgr('2', f'        on disk  sha384:{on_disk}')}\n"
        "\n"
        f"{sgr('1;31', '✘')} {sgr('1;31', 'needs attention')}\n"
        + "".join(f"  {line}\n" for line in doc["refusal"].splitlines())
    )


@pytest.mark.backend_matrix
def test_tty_status_a_database_ahead_of_the_directory(project, pkg, db, capsys, tty):
    ahead_of_the_directory(project, pkg, db)
    doc = document(capsys, db)

    code, out, _ = cli(capsys, "status", "--url", db.url)

    assert code == 4
    assert out == (
        f"{tty_line(db)}\n"
        + tty_heading("▲", "1;33", "1 ahead", table(db))
        + f"  {sgr('1;32', '✔')} 0001_create_author  "
        f"{sgr('32', 'applied')}{sgr('2', f'  {at(doc, 1)}')}\n"
        f"  {sgr('1;31', '✘')} 0002_second         "
        f"{sgr('31', 'applied, not in the directory')}\n"
        "\n"
        f"{sgr('1;31', '✘')} {sgr('1;31', 'needs attention')}\n"
        + "".join(f"  {line}\n" for line in doc["refusal"].splitlines())
    )


@pytest.mark.backend_matrix
def test_tty_status_with_only_a_refusal_heads_it_needs_attention(
    project, pkg, db, capsys, tty
):
    unreadable_directory(project, pkg, db)
    doc = document(capsys, db)

    code, out, _ = cli(capsys, "status", "--url", db.url)

    assert code == 4
    assert out == (
        f"{tty_line(db)}\n"
        + tty_heading("▲", "1;33", "needs attention", table(db))
        + "\n"
        f"{sgr('1;31', '✘')} {sgr('1;31', 'needs attention')}\n"
        + "".join(f"  {line}\n" for line in doc["refusal"].splitlines())
    )


@pytest.mark.backend_matrix
def test_no_color_keeps_the_terminal_layout_without_escapes(
    project, pkg, db, capsys, tty, monkeypatch
):
    ahead_of_the_directory(project, pkg, db)
    doc = document(capsys, db)
    monkeypatch.setenv("NO_COLOR", "1")

    code, out, _ = cli(capsys, "status", "--url", db.url)

    assert code == 4
    assert out == (
        f"{db.line}\n"
        f"▲ 1 ahead  {table(db)}\n"
        f"  ✔ 0001_create_author  applied  {at(doc, 1)}\n"
        "  ✘ 0002_second         applied, not in the directory\n"
        "\n"
        "✘ needs attention\n"
        + "".join(f"  {line}\n" for line in doc["refusal"].splitlines())
    )


@pytest.mark.backend_matrix
def test_tty_drift_findings_under_a_red_heading(project, pkg, db, capsys, tty):
    drifted(project, pkg, db)

    code, out, err = cli(capsys, "drift", "--url", db.url)

    assert code == 4
    lines = out.splitlines()
    bullet = sgr("31", "•")
    assert lines[:2] == [
        tty_line(db),
        f"{sgr('1;31', '✘')} {sgr('1;31', '2 differences against 0002_second')}",
    ]
    assert sorted(lines[2:]) == [
        f"  {bullet} idx_author_name index is extra",
        f"  {bullet} idx_author_status index is extra",
    ]
    assert err == ""


@pytest.mark.backend_matrix
def test_tty_drift_clean_is_a_green_heading(project, pkg, db, capsys, tty):
    two_applied(project, pkg, db)

    code, out, err = cli(capsys, "drift", "--url", db.url)

    assert (code, err) == (0, "")
    assert out == (
        f"{tty_line(db)}\n"
        f"{sgr('1;32', '✔')} {sgr('1;32', 'no drift')}"
        f"{sgr('2', '  against 0002_second')}\n"
    )


@pytest.mark.backend_matrix
def test_tty_drift_refusal_is_the_unchanged_sentence(project, pkg, db, capsys, tty):
    two_applied(project, pkg, db)
    db.execute("UPDATE _ferro_migrations SET finished_at = NULL WHERE migration = 2")

    code, out, err = cli(capsys, "drift", "--url", db.url)

    assert (code, out) == (4, f"{tty_line(db)}\n")
    assert err == (
        f"{sgr('1;31', '✘')} {sgr('1;31', 'drift refused')}\n"
        f"{drift_api(db).refusal}\n"
    )


@pytest.mark.postgres_only
def test_tty_drift_warnings_carry_the_warning_glyph(project, pkg, db, capsys, tty):
    foreign_policy(project, pkg, db)

    code, out, err = cli(capsys, "drift", "--url", db.url)

    assert code == 0
    assert out.splitlines()[1] == (
        f"{sgr('1;32', '✔')} {sgr('1;32', 'no drift')}{sgr('2', '  against 0001_ledger')}"
    )
    assert err == f"{sgr('1;33', '▲')}{sgr('1;33', ' warning')}  {FOREIGN_POLICY}\n"


# -- the helpers -------------------------------------------------------------------


def test_applied_at_is_the_minute_in_utc_with_z():
    assert applied_at_short("2026-10-14T09:40:12.123456Z") == "2026-10-14 09:40Z"
    assert applied_at_short("2026-10-14T11:40:12+02:00") == "2026-10-14 09:40Z"
    assert applied_at_short(None) == ""


def test_a_files_kind_is_read_from_its_suffix():
    assert kind_of_file("02_backfill_account.py") == "data"
    assert kind_of_file("01_expand.up.postgres.sql") == "ddl"
    assert kind_of_file("01_expand.down.sqlite.sql") == "ddl"
    assert kind_of_file("01_second.up.sql") == "portable_sql"
    assert kind_of_file("01_second.down.sql") == "portable_sql"
    assert kind_of_file("ir.json") == "snapshot"
    with pytest.raises(ValueError, match="notes.txt"):
        kind_of_file("notes.txt")


# -- a terminal that cannot show Unicode -------------------------------------------


def ferro_ascii(project: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """``ferro`` in a process whose stdout and stderr are ASCII, as a
    terminal set to it would be; the colour forced, so the TTY form."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from ferro.cli import main; sys.exit(main())",
            *argv,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=project,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "ascii",
            "FORCE_COLOR": "1",
            "COLUMNS": "100",
            "TERM": "xterm-256color",
        },
    )


def unstyled(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_an_ascii_terminal_gets_ascii_from_new_and_status(project, pkg, tmp_path):
    url = f"sqlite:{tmp_path / 'app.db'}?mode=rwc"
    configure(project, pkg, "sqlite")
    write_models(project, pkg, AUTHOR)

    made = ferro_ascii(project, "migrate", "new", "initial")
    assert made.returncode == 0, made.stderr
    assert unstyled(made.stdout).splitlines()[:2] == [
        "default - migrations",
        "ok Wrote migrations/0001_initial/",
    ]
    assert made.stdout.isascii() and made.stderr.isascii()

    assert run("migrate", "up", "--url", url) == 0
    status = ferro_ascii(project, "migrate", "status", "--url", url)
    assert status.returncode == 0, status.stderr
    lines = unstyled(status.stdout).splitlines()
    assert lines[:2] == [f"default - {url}", "ok 1 applied  main._ferro_migrations"]
    assert lines[2].startswith("  ok 0001_initial  applied  ")
    assert status.stdout.isascii() and status.stderr.isascii()


# -- a step file's kind -----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "role", "kind"),
    [
        ("01_expand.up.postgres.sql", ("ddl", "up", "postgres"), "ddl"),
        ("01_expand.down.sqlite.sql", ("ddl", "down", "sqlite"), "ddl"),
        ("02_fix.up.sql", ("portable_sql", "up", "portable"), "portable_sql"),
        ("02_fix.down.sql", ("portable_sql", "down", "portable"), "portable_sql"),
        ("03_backfill_author.py", ("data", "up", "portable"), "data"),
    ],
)
def test_a_step_files_kind_is_the_cores_reading_of_its_name(name, role, kind):
    """The core reads a step file's name once (the directory reader's own
    parse); the output colours by what it says, never by a second parse."""
    import json

    from ferro._core import _step_file_role
    from ferro.cli.output import kind_of_file

    said = json.loads(_step_file_role(name))
    assert (said["kind"], said["direction"], said["dialect"]) == role
    assert kind_of_file(name) == kind


def test_a_name_that_is_no_step_file_is_refused_and_ir_json_is_the_snapshot():
    from ferro.cli.output import kind_of_file

    assert kind_of_file("ir.json") == "snapshot"
    for name in ("01_expand.sideways.sql", "01_expand.up.mysql.sql", "notes.txt"):
        with pytest.raises(ValueError, match="is not a migration step file"):
            kind_of_file(name)
