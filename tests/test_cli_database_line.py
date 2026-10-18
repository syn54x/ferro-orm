# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""The database line every ``ferro migrate`` verb opens its stdout with (#648).

```text
$ ferro migrate status
main · postgresql://pinch:***@db.internal.pinch.finance:5432/pinch_prod?sslmode=require
...
```

A verb that connects names the URL it was given, its secrets masked by
``***`` (the userinfo password and every secret query value), and prints it
only once the URL is resolved and its scheme accepted. ``new`` and
``check`` never connect, so they name the migrations directory. ``init``
prints none, and ``status --json`` prints the document alone, the masked URL
in its ``url`` key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ferro.cli.output import Output
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    DIRECTORY_LINE,
    pkg,
    project,
    run,
    write_config,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    masked,
    migrations,
    new,
    sha384,
)

pytestmark = pytest.mark.usefixtures("isolated_imports", "clean_registry")

SECRET = "hunter2-secret"


def table(db) -> str:
    schema = "main" if db.backend == "sqlite" else db.schema
    return f"{schema}._ferro_migrations"


def cli(capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = run("migrate", *argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def author_generated(project: Path, pkg: str, db) -> None:
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")


def test_the_line_is_the_name_a_middle_dot_and_where():
    import io

    out = io.StringIO()
    Output.for_streams(out=out, err=io.StringIO()).database_line(
        "main", "migrations/main"
    )
    assert out.getvalue() == "main · migrations/main\n"


# -- the verbs that connect -------------------------------------------------------


@pytest.mark.backend_matrix
def test_status_opens_with_the_database_line(project, pkg, db, capsys):
    author_generated(project, pkg, db)

    code, out, _ = cli(capsys, "status", "--url", db.url)

    assert code == 3
    assert out == (
        f"{db.line}\n-- 1 pending  {table(db)}\n  -- 0001_create_author  pending\n"
    )


@pytest.mark.backend_matrix
def test_status_json_is_the_document_alone_with_the_masked_url(
    project, pkg, db, capsys
):
    author_generated(project, pkg, db)

    code, out, _ = cli(capsys, "status", "--json", "--url", db.url)

    assert code == 3
    document = json.loads(out)
    assert document["url"] == masked(db.url)
    assert document["database"] == "default"
    assert list(document)[:3] == ["database", "url", "dialect"]


@pytest.mark.backend_matrix
def test_every_connecting_verb_opens_with_the_database_line(project, pkg, db, capsys):
    author_generated(project, pkg, db)

    code, out, _ = cli(capsys, "up", "--url", db.url)
    assert code == 0
    assert out.splitlines()[0] == db.line
    assert out.splitlines()[1].startswith("0001_create_author  01_schema  applied (")

    code, out, _ = cli(capsys, "up", "--url", db.url)
    assert (code, out) == (
        0,
        f"{db.line}\nnothing to apply: the database is up to date\n",
    )

    code, out, _ = cli(capsys, "drift", "--url", db.url)
    assert (code, out) == (0, f"{db.line}\nno drift against 0001_create_author\n")

    code, out, _ = cli(capsys, "baseline", "--remove", "--url", db.url)
    assert (code, out) == (0, f"{db.line}\nno baseline to remove\n")

    up_file = migrations(project) / f"0001_create_author/01_schema.up.{db.backend}.sql"
    applied = sha384(up_file)
    up_file.write_bytes(up_file.read_bytes() + b"-- reviewed\n")
    edited = sha384(up_file)
    code, out, _ = cli(capsys, "rerecord", "0001:01", "--url", db.url)
    assert (code, out) == (
        0,
        f"{db.line}\nre-recorded 0001_create_author/{up_file.name} "
        f"(sha384:{applied} → sha384:{edited}); nothing was run\n",
    )

    code, out, _ = cli(capsys, "down", "--yes", "--url", db.url)
    assert code == 0
    lines = out.splitlines()
    assert lines[:3] == [
        db.line,
        "down reverts, in this order:",
        "  0001_create_author  01_schema",
    ]
    assert lines[3].startswith("0001_create_author  01_schema  reverted (")


# -- the verbs that never connect -------------------------------------------------


def test_new_and_check_name_the_migrations_directory(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)

    code, out, _ = cli(capsys, "new", "initial")
    assert code == 0
    assert out.splitlines()[:2] == [DIRECTORY_LINE, "ok Wrote migrations/0001_initial/"]

    code, out, _ = cli(capsys, "check")
    assert (code, out) == (0, f"{DIRECTORY_LINE}\nok: models match 0001_initial\n")


def test_a_named_database_shows_its_own_directory(project, pkg, capsys):
    (project / "ferro.toml").write_text(
        f'[databases.main]\nmodels = ["{pkg}.models"]\ndialects = ["sqlite"]\n'
        f'directory = "migrations/main"\n'
    )
    write_models(project, pkg, AUTHOR)

    code, out, _ = cli(capsys, "new", "initial")
    assert code == 0
    assert out.splitlines()[:2] == [
        "main · migrations/main",
        "ok Wrote migrations/main/0001_initial/",
    ]

    code, out, _ = cli(capsys, "check")
    assert (code, out) == (0, "main · migrations/main\nok: models match 0001_initial\n")


# -- secrets ----------------------------------------------------------------------


@pytest.mark.sqlite_only
def test_a_secret_query_value_is_masked_on_every_stream(project, pkg, tmp_path, capsys):
    configure(project, pkg, "sqlite")
    write_models(project, pkg, AUTHOR)
    new("create_author")
    url = f"sqlite:{tmp_path / 'app.db'}?mode=rwc&password={SECRET}"

    code, out, err = cli(capsys, "status", "--url", url)

    assert code == 1
    assert out == f"default · sqlite:{tmp_path / 'app.db'}?mode=rwc&password=***\n"
    assert "password=***" in err
    assert SECRET not in out
    assert SECRET not in err


@pytest.mark.sqlite_only
def test_a_sqlite_path_with_a_space_is_shown_as_configured(
    project, pkg, tmp_path, capsys
):
    """A URL with nothing to mask comes back byte for byte: the space is not
    escaped to ``%20``."""
    configure(project, pkg, "sqlite")
    write_models(project, pkg, AUTHOR)
    new("create_author")
    folder = tmp_path / "Jane Doe"
    folder.mkdir()
    url = f"sqlite:{folder / 'app.db'}?mode=rwc"

    code, out, _ = cli(capsys, "status", "--url", url)

    assert code == 3
    assert out.splitlines()[0] == f"default · {url}"


def test_a_postgres_url_masks_its_password_and_its_secret_query_values(
    project, pkg, capsys
):
    """The URL from the ticket: a ``%``-escaped password, a ``password=``
    query value, a kept ``sslmode``. Nothing listens on port 1, so the verb
    fails to connect after printing the line; neither secret reaches either
    stream."""
    configure(project, pkg, "postgres")
    write_models(project, pkg, AUTHOR)
    url = (
        f"postgresql://u:p%40{SECRET}@127.0.0.1:1/db?password={SECRET}x&sslmode=require"
    )

    code, out, err = cli(capsys, "status", "--url", url)

    assert code == 1
    assert out == (
        "default · postgresql://u:***@127.0.0.1:1/db?password=***&sslmode=require\n"
    )
    assert "u:***@" in err
    assert SECRET not in out
    assert SECRET not in err


@pytest.mark.parametrize("raw", ["?", "#", "/"])
def test_a_password_holding_a_raw_delimiter_never_reaches_either_stream(
    project, pkg, capsys, raw
):
    """A raw ``/``, ``?`` or ``#`` ends the URL's authority early, so the URL
    parser (the driver's) reads ``u:<secret>`` as a host with a bad port and
    refuses the URL. The line masks it whole, then the driver refuses it."""
    configure(project, pkg, "postgres")
    write_models(project, pkg, AUTHOR)
    url = f"postgresql://u:{SECRET}{raw}tail@127.0.0.1:1/db?sslmode=require"

    code, out, err = cli(capsys, "status", "--url", url)

    assert code == 1
    assert out == "default · postgresql://***\n"
    assert SECRET not in out
    assert "tail" not in out
    assert SECRET not in err
    assert "tail" not in err


def test_an_at_sign_in_a_query_value_never_shows_the_password_after_it(
    project, pkg, capsys
):
    """An ``@`` past the host cannot be told from the delimiter of a password
    holding a raw ``?``, so the URL is masked whole; ``%40`` shows it."""
    configure(project, pkg, "postgres")
    write_models(project, pkg, AUTHOR)
    url = f"postgresql://u:pw@127.0.0.1:1/db?application_name=a@b&password={SECRET}"

    code, out, err = cli(capsys, "status", "--url", url)

    assert code == 1
    assert out == "default · postgresql://***\n"
    assert SECRET not in out
    assert SECRET not in err

    code, out, err = cli(capsys, "status", "--url", url.replace("a@b", "a%40b"))

    assert code == 1
    assert out == (
        "default · postgresql://u:***@127.0.0.1:1/db"
        "?application_name=a%40b&password=***\n"
    )
    assert SECRET not in out
    assert SECRET not in err


# -- nothing on stdout before a refused URL ---------------------------------------


def test_an_unset_url_env_prints_nothing_on_stdout(project, pkg, capsys, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    configure(project, pkg, "sqlite")
    write_models(project, pkg, AUTHOR)

    code, out, err = cli(capsys, "status")

    assert code == 1
    assert out == ""
    assert err == (
        "database `default` reads its URL from $DATABASE_URL, which is not set; "
        "export DATABASE_URL=<url> or pass --url\n"
    )


def test_an_unsupported_scheme_prints_nothing_on_stdout(project, pkg, capsys):
    configure(project, pkg, "postgres")
    write_models(project, pkg, AUTHOR)

    code, out, err = cli(capsys, "up", "--url", f"mysql://u:{SECRET}@h/db")

    assert code == 1
    assert out == ""
    assert err == (
        "DB Connection failed for mysql://***: Unsupported database URL "
        "scheme 'mysql'. Supported schemes: sqlite, postgres, postgresql\n"
    )


@pytest.mark.parametrize(
    "url",
    [f"postgresql:/u:{SECRET}@h/db", f"postgresql:u:{SECRET}@h/db"],
    ids=["one-slash", "no-slash"],
)
def test_a_hostless_postgres_url_never_shows_its_password(project, pkg, capsys, url):
    """Both shapes parse with no host, so no password is read from them; the
    scheme is refused, and the refusal masks the URL whole."""
    configure(project, pkg, "postgres")
    write_models(project, pkg, AUTHOR)

    code, out, err = cli(capsys, "status", "--url", url)

    assert code == 1
    assert out == ""
    assert SECRET not in out + err
    assert err == (
        "DB Connection failed for postgresql://***: Unsupported database URL "
        "scheme 'postgresql'. Supported schemes: sqlite, postgres, postgresql\n"
    )
