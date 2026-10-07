"""A throwaway project for the migrations examples (not an example itself).

Each ``migrations_*.py`` example declares its models between
``# --8<-- [start:models]`` and ``# --8<-- [end:models]`` as they stand after
the change it demonstrates. :class:`Project` writes that region into a real
project as ``blog/models.py`` and drives the ``ferro`` command against a
SQLite file, the way a developer would in a terminal.

The state before the change is read off the same region: a line marked
``# new`` did not exist yet, a line marked ``# before: <text>`` read
``<text>``, and a comment line ``# before: <text>`` stands for the line
under it. So both declaration styles of an example describe their before
and after with the same lines the docs page shows.

``python docs/examples/<example>.py <directory>`` builds the project in
``<directory>`` and leaves it there (the docs tests compare the two styles'
generated migrations); with no argument it builds it in a temporary
directory and removes it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["Project", "project"]

FERRO_TOML = 'models = ["blog.models"]\ndialects = ["postgres", "sqlite"]\n'
_START = "# --8<-- [start:models]"
_END = "# --8<-- [end:models]"
_BEFORE = re.compile(r"^(?P<indent>\s*).*?#\s*before:\s*(?P<text>.*)$")
_NEW = re.compile(r"#\s*new\b")


def _region(script: Path) -> str:
    source = script.read_text()
    start = source.index(_START) + len(_START)
    return source[start : source.index(_END)].strip("\n") + "\n"


def _before(region: str) -> str:
    lines: list[str] = []
    replaced_by_comment = False
    for line in region.splitlines():
        if replaced_by_comment:
            replaced_by_comment = False
            continue
        if _NEW.search(line):
            continue
        match = _BEFORE.match(line)
        if match is None:
            lines.append(line)
            continue
        lines.append(f"{match['indent']}{match['text']}")
        # A comment line of its own stands for the line under it.
        replaced_by_comment = line.lstrip().startswith("#")
    return "\n".join(lines) + "\n"


class Project:
    """A project directory: ``ferro.toml``, ``blog/models.py``, ``migrations/``
    and the SQLite database ``app.db`` that ``$DATABASE_URL`` names."""

    def __init__(self, root: Path, script: Path, *, configured: bool) -> None:
        self.root = root
        self.region = _region(script)
        self.url = f"sqlite:{root / 'app.db'}?mode=rwc"
        (root / "blog").mkdir(parents=True)
        (root / "blog" / "__init__.py").write_text("")
        if configured:
            (root / "ferro.toml").write_text(FERRO_TOML)

    @property
    def migrations(self) -> Path:
        return self.root / "migrations"

    def models_before(self) -> None:
        """``blog/models.py`` as it stood before the change."""
        (self.root / "blog" / "models.py").write_text(_before(self.region))

    def models_after(self) -> None:
        """``blog/models.py`` as the docs page shows it."""
        (self.root / "blog" / "models.py").write_text(self.region)

    def ferro(self, *args: str, exit_code: int = 0) -> str:
        """Run ``ferro <args>`` in the project; return what it printed."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from ferro.cli import main; sys.exit(main())",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=self.root,
            env={
                **os.environ,
                "DATABASE_URL": self.url,
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        printed = result.stdout + result.stderr
        assert result.returncode == exit_code, (
            f"ferro {' '.join(args)} exited {result.returncode}, not {exit_code}:\n{printed}"
        )
        return printed

    def sql(self, statement: str, *params: object) -> list[tuple]:
        """Run one statement on the project's SQLite database."""
        import sqlite3

        with sqlite3.connect(self.root / "app.db") as connection:
            rows = connection.execute(statement, params).fetchall()
        connection.close()
        return rows


@contextmanager
def project(script: str, *, configured: bool = True) -> Iterator[Project]:
    """The project for ``script`` (the example's ``__file__``), in
    ``sys.argv[1]`` when given (kept), else in a temporary directory. With
    ``configured`` its ``ferro.toml`` is written; without, the example runs
    ``ferro migrate init`` itself."""
    path = Path(script).resolve()
    if len(sys.argv) > 1:
        root = Path(sys.argv[1]).resolve()
        if root.exists():
            shutil.rmtree(root)
        yield Project(root, path, configured=configured)
        return
    with tempfile.TemporaryDirectory() as tmp:
        yield Project(Path(tmp) / "app", path, configured=configured)


if __name__ == "__main__":
    print(__doc__.splitlines()[0])
