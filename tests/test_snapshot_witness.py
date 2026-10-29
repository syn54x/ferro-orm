# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""A squash's witness in ``ir.json`` (#668, ADR-0056): schema IR version 3.

The witness is the ``replaces`` list beside ``parent_checksum``: one entry
per replaced migration, nesting a replaced squash's own. Nothing writes one
yet but these tests (the ``squash`` verb is #669), so each test writes it by
hand into a project ``ferro migrate new`` generated, and reads it back through
the FFI and through ``check``, ``status`` and ``up``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ferro import _core
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    LIBRARY,
    pkg,
    project,
    run,
    write_config,
    write_models,
)
from tests.test_migrate_up import db, new  # noqa: F401 - fixture

pytestmark = pytest.mark.usefixtures("isolated_imports", "clean_registry")

VECTOR = (
    Path(__file__).parent / "fixtures" / "ir_vectors" / "schema_squash_witness_v3.json"
)


def migrations(project: Path) -> Path:
    return project / "migrations"


def sha384(path: Path) -> str:
    return hashlib.sha384(path.read_bytes()).hexdigest()


def store(path: Path, ir: dict[str, Any]) -> None:
    """Write ``ir`` the way ``Snapshot::store`` does: sorted keys, two-space
    indent, a trailing newline."""
    path.write_text(json.dumps(ir, indent=2, sort_keys=True) + "\n")


def add_witness(snapshot: Path, replaces: list[dict[str, Any]]) -> None:
    ir = json.loads(snapshot.read_text())
    ir["replaces"] = replaces
    store(snapshot, ir)


def entry(migration: str, checksum: str, steps: list[int]) -> dict[str, Any]:
    return {
        "migration": migration,
        "snapshot_checksum": checksum,
        "steps": steps,
        "replaces": [],
    }


def two_migrations(project: Path, pkg: str, dialects: str) -> None:
    write_config(project, pkg, dialects)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, LIBRARY)
    new("create_post")


def dialect_list(backend: str) -> str:
    return f'["{backend}"]'


# -- the format ----------------------------------------------------------------------


def test_new_writes_a_v4_snapshot_without_a_witness(project, pkg):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    new("create_author")

    snapshot = migrations(project) / "0001_create_author/ir.json"
    ir = json.loads(snapshot.read_text())
    assert ir["ir_version"] == 4
    assert "replaces" not in ir

    loaded = json.loads(_core._load_snapshot(snapshot.read_text()))
    assert loaded["replaces"] == []
    (migration,) = json.loads(_core._read_migrations_dir(str(migrations(project))))[
        "migrations"
    ]
    assert migration["replaces"] == []
    assert migration["snapshot"]["replaces"] == []


def test_the_v3_vector_loads_with_its_nested_witness():
    ir = json.loads(VECTOR.read_text())["ir"]
    loaded = json.loads(_core._load_snapshot(json.dumps(ir)))

    def names(witness: list[dict[str, Any]]) -> list[Any]:
        return [(e["migration"], e["steps"], names(e["replaces"])) for e in witness]

    assert names(loaded["replaces"]) == [
        (
            "0005_teams_epic",
            [1, 2],
            [("0005_add_teams", [1], []), ("0006_team_slug", [1, 2], [])],
        ),
        ("0006_drop_tmp", [1], []),
    ]
    assert loaded["replaces"] == ir["replaces"]
    assert loaded["ir"]["ir_version"] == 3


def test_a_v2_envelope_carrying_replaces_is_refused():
    ir = json.loads(VECTOR.read_text())["ir"]
    ir["ir_version"] = 2
    with pytest.raises(ValueError) as refused:
        _core._load_snapshot(json.dumps(ir))
    message = str(refused.value)
    assert "v2" in message and "replaces" in message, message


# -- the directory reader, through every verb ----------------------------------------


@pytest.mark.backend_matrix
def test_a_hand_written_witness_loads_under_every_verb(project, pkg, db, capsys):
    """Two originals, applied to another database, then removed; the one
    migration generated in their place carries a well-formed witness naming
    them. The reader checks the witness against the directory only, so the
    database under test (which never ran the originals; inheriting them is
    #671's) reads it without a refusal."""
    two_migrations(project, pkg, '["postgres", "sqlite"]')
    elsewhere = f"sqlite:{project / 'originals.db'}?mode=rwc"
    assert run("migrate", "up", "--url", elsewhere) == 0
    dirs = sorted(d for d in migrations(project).iterdir() if d.name[0].isdigit())
    originals = [
        entry(
            dir.name,
            sha384(dir / "ir.json"),
            sorted({int(f.name[:2]) for f in dir.iterdir() if f.name != "ir.json"}),
        )
        for dir in dirs
    ]
    for dir in dirs:
        for file in dir.iterdir():
            file.unlink()
        dir.rmdir()
    new("library")
    add_witness(migrations(project) / "0001_library/ir.json", originals)
    capsys.readouterr()

    assert run("migrate", "check") == 0, capsys.readouterr().err
    # Pending (status exits 3), not refused.
    assert run("migrate", "status", "--url", db.url) == 3
    out = capsys.readouterr().out
    assert "0001_library  pending" in out and "needs attention" not in out, out
    assert run("migrate", "up", "--url", db.url) == 0, capsys.readouterr().err
    assert run("migrate", "status", "--url", db.url) == 0, capsys.readouterr()

    (migration,) = json.loads(_core._read_migrations_dir(str(migrations(project))))[
        "migrations"
    ]
    assert [e["migration"] for e in migration["replaces"]] == [
        "0001_create_author",
        "0002_create_post",
    ]
    assert migration["replaces"] == migration["snapshot"]["replaces"]


def _refused_by_every_verb(db, capsys, kind: str, *needles: str) -> None:
    capsys.readouterr()
    assert run("migrate", "check") == 3
    err = capsys.readouterr().err
    assert err.startswith(f"{kind}: "), err
    for needle in needles:
        assert needle in err, err

    assert run("migrate", "up", "--url", db.url) == 1
    err = capsys.readouterr().err
    for needle in needles:
        assert needle in err, err

    assert run("migrate", "status", "--url", db.url) == 4
    out = capsys.readouterr().out
    assert "needs attention" in out, out
    for needle in needles:
        assert needle in out, out


def squash_with_an_original_left(project: Path, pkg: str, backend: str, left: str):
    """The originals ``0001_create_author`` and ``0002_create_post`` replaced
    by the squash ``0001_library`` (a squash takes its first original's
    number and parent), its witness naming both, with the original ``left``
    put back beside it."""
    two_migrations(project, pkg, dialect_list(backend))
    aside = project / "aside"
    aside.mkdir()
    originals = []
    for name in ("0001_create_author", "0002_create_post"):
        original = migrations(project) / name
        originals.append(entry(name, sha384(original / "ir.json"), [1]))
        original.rename(aside / name)
    new("library")
    add_witness(migrations(project) / "0001_library/ir.json", originals)
    (aside / left).rename(migrations(project) / left)


@pytest.mark.backend_matrix
@pytest.mark.parametrize(
    "left",
    # 0002_create_post shares no number but would break the chain;
    # 0001_create_author shares the squash's number.
    ["0002_create_post", "0001_create_author"],
)
def test_an_original_left_beside_its_squash_is_refused_by_every_verb_naming_both(
    project, pkg, db, capsys, left
):
    squash_with_an_original_left(project, pkg, db.backend, left)
    refusal = (
        f"0001_library/ir.json records that the squash 0001_library replaces {left}, "
        f"but {left} is still in the migrations directory; delete it, or delete the "
        f"squash 0001_library"
    )

    with pytest.raises(ValueError) as refused:
        _core._read_migrations_dir(str(migrations(project)))
    assert str(refused.value) == refusal
    for target in ("0001_library", left):
        for keyword in ("replacing", "range_from"):
            with pytest.raises(ValueError) as refused:
                _core._read_migrations_dir(
                    str(migrations(project)), **{keyword: target}
                )
            assert str(refused.value) == refusal, (keyword, target)

    capsys.readouterr()
    assert run("migrate", "check") == 3
    assert capsys.readouterr().err == f"witness_names_present: {refusal}\n"

    expected = f"ferro migrate: {refusal}. Nothing was applied."
    assert run("migrate", "up", "--url", db.url) == 1
    assert capsys.readouterr().err == expected + "\n"

    assert run("migrate", "status", "--url", db.url) == 4
    assert f"needs attention\n  {expected}\n" in capsys.readouterr().out


@pytest.mark.backend_matrix
@pytest.mark.parametrize(
    "malformed",
    ["a 95-character checksum", "no steps", "a nested repeat"],
)
def test_a_malformed_witness_is_refused_naming_its_file(
    project, pkg, db, capsys, malformed
):
    two_migrations(project, pkg, dialect_list(db.backend))
    good = entry("0009_gone", "ab" * 48, [1])
    if malformed == "a 95-character checksum":
        witness = [{**good, "snapshot_checksum": "ab" * 47 + "a"}]
    elif malformed == "no steps":
        witness = [{**good, "steps": []}]
    else:
        witness = [{**good, "replaces": [dict(good)]}]
    add_witness(migrations(project) / "0002_create_post/ir.json", witness)

    _refused_by_every_verb(
        db,
        capsys,
        "unreadable_snapshot",
        "0002_create_post/ir.json",
        "restore the file",
    )
