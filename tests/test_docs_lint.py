"""The docs lint (``tests._docs_checks``) as a test gate.

It runs over every docs page, example script and docstring in ``src/ferro``
and must find nothing; each check also fails on a seeded example of the
mistake it catches.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests._docs_checks import lambda_param_violations, main, unfailing_error_checks

REPO_ROOT = Path(__file__).resolve().parents[1]
LINTED = ("docs/pages", "docs/examples", "src/ferro")


def test_docs_pages_examples_and_docstrings_pass_the_lint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    status = main(list(LINTED))
    assert capsys.readouterr().out == ""
    assert status == 0


def test_command_line_exits_zero_on_the_tree() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tests._docs_checks", *LINTED],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _page(tmp_path: Path, body: str) -> Path:
    page = tmp_path / "page.md"
    page.write_text(f"# Page\n\nText:\n\n```python\n{body}\n```\n", encoding="utf-8")
    return page


def test_one_letter_lambda_on_a_page_is_reported(tmp_path: Path) -> None:
    page = _page(tmp_path, "posts = await Post.where(lambda t: t.id == 1).all()")
    [violation] = lambda_param_violations(page)
    assert violation.line == 6
    assert "one-letter parameter 't'" in violation.message
    assert main([str(page)]) == 1


def test_one_letter_lambda_in_a_docstring_is_reported(tmp_path: Path) -> None:
    module = tmp_path / "library.py"
    module.write_text(
        'def first():\n    """Return one.\n\n    ```python\n'
        "    await Post.order_by(lambda t: t.id).first()\n"
        '    ```\n    """\n',
        encoding="utf-8",
    )
    [violation] = lambda_param_violations(module)
    assert violation.line == 5


def test_error_check_that_cannot_fail_is_reported(tmp_path: Path) -> None:
    page = _page(
        tmp_path,
        "try:\n"
        "    await Post.create(title=None)\n"
        "except ValueError as error:\n"
        '    assert "title" in str(error)',
    )
    [violation] = unfailing_error_checks(page)
    assert 'else: raise AssertionError("expected ValueError")' in violation.message


def test_named_lambda_and_checked_error_pass(tmp_path: Path) -> None:
    page = _page(
        tmp_path,
        "posts = await Post.where(lambda post: post.id == 1).all()\n"
        "try:\n"
        "    await Post.create(title=None)\n"
        "except ValueError as error:\n"
        '    assert "title" in str(error)\n'
        "else:\n"
        '    raise AssertionError("expected ValueError")',
    )
    assert main([str(page)]) == 0
