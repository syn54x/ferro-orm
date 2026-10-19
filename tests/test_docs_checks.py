"""The docs checks: one-letter query lambdas and error checks that cannot fail."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from tests._docs_checks import (
    Violation,
    lambda_param_violations,
    query_methods_taking_callables,
    unfailing_error_checks,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def test_two_letter_lambda_parameter_passes(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "ok.py",
        """
        authors = Author.where(lambda author: author.id == 1)
        """,
    )
    assert lambda_param_violations(path) == []


def test_one_letter_lambda_parameter_fails_with_its_line(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "bad.py",
        """
        import ferro

        authors = Author.where(lambda a: a.id == 1)
        """,
    )
    assert lambda_param_violations(path) == [
        Violation(
            path,
            3,
            "lambda passed to where() takes one-letter parameter 'a'; "
            "name it after the model in lowercase singular",
        )
    ]


def test_lambda_passed_to_sorted_is_ignored(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "sorted.py",
        """
        names = sorted(rows, key=lambda r: r.name)
        rows.sort(key=lambda r: r.name)
        """,
    )
    assert lambda_param_violations(path) == []


def test_every_named_query_method_is_checked(tmp_path: Path) -> None:
    methods = query_methods_taking_callables()
    # Derived from the Query and Model signatures, plus the names the
    # docs use on aggregate projections.
    assert {
        "where",
        "select",
        "order_by",
        "include",
        "join",
        "left_join",
        "update",
        "exists",
        "count",
        "aggregate",
        "group_by",
        "after",
        "before",
    } <= methods
    lines = "\n".join(f"q.{name}(lambda x: x.id)" for name in sorted(methods))
    path = _write(tmp_path, "all.py", lines + "\n")
    assert [v.line for v in lambda_param_violations(path)] == list(
        range(1, len(methods) + 1)
    )


def test_markdown_fences_are_checked_at_their_page_line(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "page.md",
        """
        # Page

        === "Tab"

            ```python
            posts = await Post.where(lambda p: p.published).all()
            ```

        ```text
        Post.where(lambda p: p.published)
        ```
        """,
    )
    assert [(v.line, "'p'" in v.message) for v in lambda_param_violations(path)] == [
        (6, True)
    ]


def test_docstring_fences_are_checked_at_their_file_line(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "module.py",
        '''
        """A module.

        ```python
        posts = await Post.where(lambda post: post.published).all()
        ```
        """


        def helper():
            """Return the published posts.

            Examples:
                ```python
                posts = await Post.where(lambda p: p.published).all()
                ```
            """
        ''',
    )
    assert [v.line for v in lambda_param_violations(path)] == [14]


def test_except_that_asserts_without_else_is_reported(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "errors.py",
        """
        try:
            Post.where(lambda post: post.views.sum() > 100)
        except TypeError as exc:
            assert "having()" in str(exc)
        """,
    )
    assert unfailing_error_checks(path) == [
        Violation(
            path,
            1,
            "try/except asserts on the error but has no else that raises, "
            "so it passes when the error stops happening; add "
            'else: raise AssertionError("expected TypeError")',
        )
    ]


def test_except_that_asserts_with_raising_else_is_not_reported(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        "errors.py",
        """
        try:
            Post.where(lambda post: post.views.sum() > 100)
        except TypeError as exc:
            assert "having()" in str(exc)
        else:
            raise AssertionError("expected TypeError")
        """,
    )
    assert unfailing_error_checks(path) == []


def test_except_without_assert_is_not_an_error_check(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "cleanup.py",
        """
        try:
            import tomllib
        except ImportError:
            tomllib = None
        """,
    )
    assert unfailing_error_checks(path) == []


def test_cli_lists_violations_and_exits_1(tmp_path: Path) -> None:
    _write(tmp_path, "good.py", "Post.where(lambda post: post.id)\n")
    bad = _write(tmp_path, "bad.py", "Post.where(lambda p: p.id)\n")
    result = subprocess.run(
        [sys.executable, "-m", "tests._docs_checks", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 1, result.stderr
    assert result.stdout.splitlines() == [
        f"{bad}:1: lambda passed to where() takes one-letter parameter 'p'; "
        "name it after the model in lowercase singular"
    ]


def test_cli_exits_0_when_clean(tmp_path: Path) -> None:
    good = _write(tmp_path, "good.py", "Post.where(lambda post: post.id)\n")
    result = subprocess.run(
        [sys.executable, "-m", "tests._docs_checks", str(good)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert (result.returncode, result.stdout) == (0, ""), result.stderr

