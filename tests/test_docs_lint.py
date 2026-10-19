"""The docs lint (``tests._docs_checks``) as a gate over the whole tree.

``python -m tests._docs_checks docs/pages docs/examples src/ferro`` must find
nothing: no query lambda with a one-letter parameter and no error check that
cannot fail, on any docs page, in any example script or in any docstring.
``tests/test_docs_checks.py`` covers each check case by case.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LINTED = ("docs/pages", "docs/examples", "src/ferro")


def _lint(*paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tests._docs_checks", *paths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_docs_pages_examples_and_docstrings_pass_the_lint() -> None:
    result = _lint(*LINTED)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_one_letter_lambda_on_a_page_fails_the_lint(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text(
        "# Page\n\nThe first post:\n\n```python\n"
        "post = await Post.where(lambda t: t.id == 1).first()\n```\n",
        encoding="utf-8",
    )
    result = _lint(str(page))
    assert result.returncode == 1
    assert f"{page}:6: lambda passed to where() takes one-letter parameter 't'" in (
        result.stdout
    )
