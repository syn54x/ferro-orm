"""The docs lint (``tests._docs_checks``) as a gate over the whole tree.

``python -m tests._docs_checks docs/pages docs/examples src/ferro GLOSSARY.md
docs/adr README.md AGENTS.md`` must find nothing: no query lambda with a
one-letter parameter, no error check that cannot fail, no retired relation
term, no awaited relation and no model that declares a column after a
relation or runs its relations into its columns, on any docs page, in any
example script, in any docstring, in the glossary, in a current ADR, in the
README or in AGENTS.md.
``tests/test_docs_checks.py`` covers each check case by case.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LINTED = (
    "docs/pages",
    "docs/examples",
    "src/ferro",
    "GLOSSARY.md",
    "docs/adr",
    "README.md",
    "AGENTS.md",
)


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


def test_a_relation_among_the_columns_on_a_page_fails_the_lint(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text(
        "# Page\n\n```python\nclass Note(Model):\n"
        "    account_id: int = Field(foreign_key=Account)\n"
        '    account: Relation[Account] = Forward("account_id")\n'
        "    body: str\n```\n",
        encoding="utf-8",
    )
    result = _lint(str(page))
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        f"{page}:6: relation 'account' follows the columns with no blank line; "
        "declare every column first, then one blank line, then the relations",
        f"{page}:7: column 'body' is declared after relation 'account'; "
        "declare every column first, then one blank line, then the relations",
    ]
