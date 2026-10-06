"""The reconciliation pass, pinned by its recorded output (#517).

Every test in the files below drives ``connect(...)`` / ``migrate(...)`` over
a pre-shaped live database. Run under the recorder plugin
(``tests/fixtures/pass_recording/recorder.py``), each test's executed
statements and ``ferro auto-migrate:`` warnings are collected in order and
compared against the recording committed in
``tests/fixtures/pass_recording/<file>.<dialect>.json``, which was taken
before the pass was rebuilt on the one planner. A difference is a behaviour
change of the pass — a statement added, lost or reordered, or a warning
reworded — and fails here even when the file's own assertions still pass.

Re-record (only for an intended behaviour change) with
``FERRO_PASS_RECORD=1 uv run pytest tests/test_pass_recording.py
--db-backends=sqlite,postgres``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import _available_postgres_url

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pass_recording"

RECORDED_FILES = (
    "test_auto_migrate",
    "test_label_addition",
    "test_row_security_alembic",
    "test_row_security_create_pass",
    "test_row_security_orphans",
    "test_row_security_rebuild",
    "test_row_security_reconcile",
    "test_table_check_orphans",
    "test_table_check_rebuild",
    "test_table_check_reconcile",
)
DIALECTS = ("sqlite", "postgres")


def _record_pass(
    test_file: str, dialect: str, out: Path
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["FERRO_PASS_RECORDING_OUT"] = str(out)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"tests/{test_file}.py",
            "-p",
            "tests.fixtures.pass_recording.recorder",
            f"--db-backends={dialect}",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("test_file", RECORDED_FILES)
def test_the_pass_executes_and_warns_exactly_what_was_recorded(
    test_file: str, dialect: str, tmp_path: Path
):
    if dialect == "postgres" and not _available_postgres_url():
        pytest.skip("FERRO_POSTGRES_URL is not set; the Postgres recording cannot run.")

    out = tmp_path / "recording.json"
    run = _record_pass(test_file, dialect, out)
    assert run.returncode == 0, (
        f"tests/{test_file}.py failed under the recorder:\n{run.stdout[-4000:]}"
        f"\n{run.stderr[-2000:]}"
    )
    recorded = json.loads(out.read_text())
    fixture = FIXTURES / f"{test_file}.{dialect}.json"

    if os.environ.get("FERRO_PASS_RECORD") == "1":
        fixture.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n")
        return

    expected = json.loads(fixture.read_text())
    assert sorted(recorded) == sorted(expected), (
        "the set of tests that touch the pass changed"
    )
    for nodeid, run_expected in expected.items():
        assert recorded[nodeid] == run_expected, nodeid
