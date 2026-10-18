# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""A database that ran a squash's originals adopts the squash (#671, ADR-0056).

```text
$ ferro migrate squash epic --from 0002     # replaces 0002_add_tmp … 0004_drop_tmp
$ ferro migrate up                          # on a database that applied them
default · sqlite:app.db
adopted 0002_epic: this database had applied 0002_add_tmp … 0004_drop_tmp; nothing was run
```

Every run, ``up`` and ``down``, adopts under the run lock before it plans:
the tracking table alone is the witness (finished records for exactly the
ordinals the squash's ``ir.json`` records, under the snapshot checksums it
records), and one transaction swaps those records for one ``adopted``
record per squashed step. Nothing runs. Every test builds a real project
under ``tmp_path``, applies the originals with ``ferro migrate up``, swaps
the directory for the squash with ``ferro migrate squash`` and reads the
tracking table back with a plain driver, on SQLite and Postgres.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from importlib.metadata import version as installed
from pathlib import Path

import pytest

from ferro import _core, connect
from ferro.migrations import MigrationRefused, runner, status
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    pkg,
    project,
    run,
    write_models,
)
from tests.test_migrate_squash import (  # noqa: F401 - fixtures
    BIO,
    FILLED,
    TMP,
    TMP_BIO,
    no_bytecode,
    squash,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    migrations,
    new,
    sha384,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry", "no_bytecode"),
    pytest.mark.backend_matrix,
]

ORIGINALS = ["0002_add_tmp", "0003_add_bio", "0004_drop_tmp"]
ADOPTED = (
    "adopted 0002_epic: this database had applied 0002_add_tmp … 0004_drop_tmp; "
    "nothing was run"
)
TITLE = BIO + "    title: str | None = None\n"


# -- the project and the database -------------------------------------------------


def epic(project: Path, pkg: str, db, *, apply_through: int = 4) -> None:
    """``0001_create_author``; the epic's originals ``0002_add_tmp`` (adds
    ``tmp``), ``0003_add_bio`` (adds ``bio`` with a filled-in data step) and
    ``0004_drop_tmp``; each applied by ``ferro migrate up`` up to
    ``apply_through``."""
    configure(project, pkg, db.backend)
    for number, (models, name, *flags) in enumerate(
        [
            (AUTHOR, "create_author"),
            (TMP, "add_tmp"),
            (TMP_BIO, "add_bio", "--data-step", "Author"),
            (BIO, "drop_tmp"),
        ],
        start=1,
    ):
        write_models(project, pkg, models)
        new(name, *flags)
        if name == "add_bio":
            [data] = (migrations(project) / "0003_add_bio").glob("*_backfill_*.py")
            data.write_text(FILLED)
        if number <= apply_through:
            assert run("migrate", "up", "--url", db.url) == 0


def squashed(project: Path, pkg: str, db, *, apply_through: int = 4) -> None:
    """:func:`epic`, then ``ferro migrate squash epic --from 0002``: the
    directory holds ``0001_create_author`` and ``0002_epic``."""
    epic(project, pkg, db, apply_through=apply_through)
    assert squash("epic", "--from", "0002") == 0
    assert sorted(p.name for p in migrations(project).iterdir() if p.is_dir()) == [
        "0001_create_author",
        "0002_epic",
    ]


def cli(capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = run("migrate", *argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def records(db) -> list[tuple]:
    """Every record: migration, step, name, file, kind, checksum, snapshot
    checksum, origin, ferro version, class, and whether it finished."""
    return db.rows(
        "SELECT migration, step, migration_name, file, kind, checksum, "
        "snapshot_checksum, origin, ferro_version, class, finished_at IS NOT NULL "
        "FROM _ferro_migrations ORDER BY migration, step"
    )


def names(db) -> list[tuple[str, int, str]]:
    return [(r[2], r[1], r[7]) for r in records(db)]


async def _read_live(url: str, declared: dict) -> tuple[str, str]:
    name = f"adopt_{uuid.uuid4().hex}"
    await connect(url, name=name)
    try:
        return await _core._live_schema_ir(name, json.dumps(declared), "[]")
    finally:
        await _core._disconnect(name)


def live(project: Path, db) -> tuple[str, str]:
    """The live schema and facts, read as the reconciliation pass reads them,
    planned against the squash's snapshot."""
    declared = json.loads((migrations(project) / "0002_epic" / "ir.json").read_text())
    return asyncio.run(_read_live(db.url, declared))


def expected_classes(db) -> dict[int, str]:
    """The class a run would record for each squashed step: what ``status``
    reports for a step with no record (ADR-0054)."""
    report = asyncio.run(status(url=db.url))
    [squash] = [m for m in report.migrations if m.name == "0002_epic"]
    return {step.step: step.step_class for step in squash.steps}


# -- up adopts ----------------------------------------------------------------------


def test_up_adopts_the_squash_running_nothing_and_records_its_steps(
    project, pkg, db, capsys
):
    squashed(project, pkg, db)
    classes = expected_classes(db)
    before = live(project, db)
    first = records(db)[0]

    code, out, err = cli(capsys, "up", "--url", db.url)

    assert (code, err) == (0, "")
    assert out == f"{db.line}\n{ADOPTED}\n"
    assert live(project, db) == before
    squash = migrations(project) / "0002_epic"
    snapshot = sha384(squash / "ir.json")
    rows = records(db)
    assert rows[0] == first
    adopted = rows[1:]
    steps = sorted(
        p
        for p in squash.iterdir()
        if p.name != "ir.json"
        and ".down." not in p.name
        and (p.suffix == ".py" or p.name.endswith((f".{db.backend}.sql", ".up.sql")))
    )
    assert [r[3] for r in adopted] == [p.name for p in steps]
    for row, path in zip(adopted, steps, strict=True):
        number, step, name, file, kind, checksum, snap, origin, ran_by, klass, done = (
            row
        )
        assert (number, name, origin, ran_by, done) == (
            2,
            "0002_epic",
            "adopted",
            installed("ferro-orm"),
            True,
        )
        assert checksum == sha384(path)
        assert snap == snapshot
        assert klass == classes[step]
        assert kind == ("atomic" if path.suffix == ".py" else "ddl")
    assert not {r[2] for r in rows} & set(ORIGINALS)

    # Adopted once: the next up finds nothing to adopt and nothing pending.
    code, out, _ = cli(capsys, "up", "--url", db.url)
    assert (code, out) == (
        0,
        f"{db.line}\nnothing to apply: the database is up to date\n",
    )
    assert records(db) == rows

    code, out, _ = cli(capsys, "status", "--json", "--url", db.url)
    assert code == 0
    document = json.loads(out)
    assert [(m["name"], m["state"]) for m in document["migrations"]] == [
        ("0001_create_author", "applied"),
        ("0002_epic", "applied (adopted)"),
    ]


def test_up_reports_the_adoption_and_tells_it_before_any_step(project, pkg, db):
    squashed(project, pkg, db)
    write_models(project, pkg, TITLE)
    new("add_title")
    events: list[runner.RunEvent] = []

    report = asyncio.run(runner.up(url=db.url, on_event=events.append))

    assert report.adopted == [
        runner.AdoptedMigration("0002_epic", ORIGINALS),
    ]
    assert isinstance(events[0], runner.AdoptedEvent)
    assert events[0].line() == ADOPTED
    assert [type(e) for e in events[1:]] == [runner.StepEvent]
    assert [(s.migration, s.outcome) for s in events[1:]] == [
        ("0003_add_title", "applied")
    ]
    assert [step.migration for step in report.applied] == ["0003_add_title"]
    assert names(db)[-1] == ("0003_add_title", 1, "run")


def test_a_baselined_originals_database_adopts(project, pkg, db, capsys):
    epic(project, pkg, db, apply_through=0)

    async def build() -> None:
        await connect(db.url, auto_migrate=True, name="built")
        await _core._disconnect("built")

    asyncio.run(build())
    assert cli(capsys, "baseline", "--url", db.url)[0] == 0
    assert {r[7] for r in records(db)} == {"baseline"}
    assert squash("epic", "--from", "0002") == 0

    code, out, _ = cli(capsys, "up", "--url", db.url)

    assert (code, out) == (0, f"{db.line}\n{ADOPTED}\n")
    assert [(name, origin) for name, _, origin in names(db)] == [
        ("0001_create_author", "baseline"),
        *[("0002_epic", "adopted")] * (len(records(db)) - 1),
    ]
    code, out, _ = cli(capsys, "status", "--json", "--url", db.url)
    states = [
        step["state"]
        for migration in json.loads(out)["migrations"]
        for step in migration["steps"]
    ]
    assert states[0] == "applied (baseline)"
    assert set(states[1:]) == {"applied (adopted)"}


def test_an_original_edited_after_apply_and_never_rerecorded_adopts(
    project, pkg, db, capsys
):
    epic(project, pkg, db)
    [data] = (migrations(project) / "0003_add_bio").glob("*_backfill_*.py")
    data.write_text(FILLED + "\n# edited after it was applied\n")
    code, out, _ = cli(capsys, "status", "--url", db.url)
    assert "different checksum" in out, out
    assert squash("epic", "--from", "0002") == 0

    code, out, _ = cli(capsys, "up", "--url", db.url)

    assert (code, out) == (0, f"{db.line}\n{ADOPTED}\n")


# -- down adopts first --------------------------------------------------------------


def test_down_adopts_then_reverts_through_the_squashed_downs(project, pkg, db, capsys):
    squashed(project, pkg, db)
    report = asyncio.run(runner.plan_down(url=db.url, target="0001"))
    assert report.refusal is None
    assert {step.migration for step in report.steps} == {"0002_epic"}

    code, out, err = cli(capsys, "down", "--to", "0001", "--yes", "--url", db.url)

    assert (code, err) == (0, ""), err
    lines = out.splitlines()
    adopted = lines.index(ADOPTED)
    reverted = [i for i, line in enumerate(lines) if " reverted (" in line]
    assert reverted and adopted < reverted[0], out
    assert all(lines[i].startswith("0002_epic") for i in reverted)
    assert names(db) == [("0001_create_author", 1, "run")]
    assert "bio" not in json.dumps(json.loads(live(project, db)[0]))


def test_down_to_a_step_stops_inside_the_squash(project, pkg, db, capsys):
    squashed(project, pkg, db)

    code, out, err = cli(capsys, "down", "--to", "0002:01", "--yes", "--url", db.url)

    assert (code, err) == (0, ""), err
    assert ADOPTED in out.splitlines()
    assert names(db) == [
        ("0001_create_author", 1, "run"),
        ("0002_epic", 1, "adopted"),
    ]


# -- a partial witness is refused ---------------------------------------------------

PARTIAL = (
    "ferro migrate: 0002_epic replaces migrations this database applied only part "
    "of: it applied 0002_add_tmp and 0003_add_bio but not 0004_drop_tmp, so it "
    "cannot adopt 0002_epic. To keep its rows, check out the commit that holds "
    "0004_drop_tmp, run `ferro migrate up` there to finish it, then run `ferro "
    "migrate up` here, which adopts 0002_epic. Or revert instead: on the commit "
    "that applied them run `ferro migrate down --to 0001`, then `ferro migrate up` "
    "here; the revert drops what they created and fails on an irreversible step. "
    "Nothing was changed."
)


def test_a_database_that_applied_part_of_the_originals_is_refused_naming_both_fixes(
    project, pkg, db, capsys
):
    squashed(project, pkg, db, apply_through=3)
    before = records(db)

    with pytest.raises(MigrationRefused) as raised:
        asyncio.run(runner.up(url=db.url))

    refused = raised.value.report.refused
    assert refused.kind == "partial_witness"
    assert str(refused) == PARTIAL
    assert PARTIAL.index("check out the commit") < PARTIAL.index("down --to 0001")
    assert raised.value.report.adopted == []
    assert records(db) == before

    code, out, err = cli(capsys, "up", "--url", db.url)
    assert (code, out) == (1, f"{db.line}\n")
    assert PARTIAL in err
    code, _, err = cli(capsys, "down", "--to", "0001", "--yes", "--url", db.url)
    assert code == 1 and PARTIAL in err
    assert records(db) == before


def test_a_failed_last_original_step_is_missing_from_the_witness(project, pkg, db):
    epic(project, pkg, db)
    db.execute(
        "UPDATE _ferro_migrations SET finished_at = NULL, failed_at = started_at, "
        "error = 'boom' WHERE migration = 4"
    )
    assert squash("epic", "--from", "0002") == 0
    before = records(db)

    with pytest.raises(MigrationRefused) as raised:
        asyncio.run(runner.up(url=db.url))

    assert raised.value.report.refused.kind == "partial_witness"
    assert str(raised.value.report.refused) == PARTIAL
    assert records(db) == before


# -- a squash of a squash -----------------------------------------------------------


@pytest.mark.parametrize("adopted_first", [True, False], ids=["adopted", "originals"])
def test_a_squash_of_a_squash_adopts_from_either_generation(
    project, pkg, db, capsys, adopted_first
):
    epic(project, pkg, db)
    assert squash("bio_epic", "--from", "0003") == 0
    if adopted_first:
        code, out, _ = cli(capsys, "up", "--url", db.url)
        assert out.splitlines()[1] == (
            "adopted 0003_bio_epic: this database had applied 0003_add_bio … "
            "0004_drop_tmp; nothing was run"
        )
    assert squash("epic", "--from", "0002") == 0

    code, out, _ = cli(capsys, "up", "--url", db.url)

    replaced = (
        "0002_add_tmp … 0003_bio_epic"
        if adopted_first
        else "0002_add_tmp … 0004_drop_tmp"
    )
    assert (code, out) == (
        0,
        f"{db.line}\nadopted 0002_epic: this database had applied {replaced}; "
        "nothing was run\n",
    )
    assert {name for name, _, _ in names(db)} == {"0001_create_author", "0002_epic"}
    assert {origin for name, _, origin in names(db) if name == "0002_epic"} == {
        "adopted"
    }


# -- two runs at once ----------------------------------------------------------------


def test_two_concurrent_ups_adopt_once(project, pkg, db):
    squashed(project, pkg, db)

    async def both() -> list[runner.RunReport]:
        return await asyncio.gather(runner.up(url=db.url), runner.up(url=db.url))

    first, second = asyncio.run(both())

    adopted = sorted([first.adopted, second.adopted], key=len)
    assert adopted == [[], [runner.AdoptedMigration("0002_epic", ORIGINALS)]]
    assert first.applied == second.applied == []
    rows = names(db)
    assert len(rows) == len(set(rows))
    assert {name for name, _, _ in rows} == {"0001_create_author", "0002_epic"}
