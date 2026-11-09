"""The docs checks: one-letter query lambdas, error checks that cannot fail and
retired relation terms."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from tests._docs_checks import (
    Violation,
    lambda_param_violations,
    query_methods_taking_callables,
    retired_vocabulary,
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


def test_parameters_captured_by_default_are_not_query_parameters(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        "capture.py",
        """
        Post.where(lambda post, f=field, v=value: getattr(post, f) == v)
        Post.where(lambda p, f=field: getattr(p, f))
        """,
    )
    assert [(v.line, "'p'" in v.message) for v in lambda_param_violations(path)] == [
        (2, True)
    ]


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


# -- retired vocabulary -------------------------------------------------------------


def _lint(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tests._docs_checks", *map(str, paths)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_shadow_column_in_prose_fails_naming_its_replacement(tmp_path: Path) -> None:
    page = _write(
        tmp_path,
        "page.md",
        """
        # Page

        Ferro adds the shadow column for you.
        """,
    )
    result = _lint(page)
    assert result.returncode == 1, result.stderr
    assert result.stdout.splitlines() == [
        f'{page}:3: "shadow column" is retired; write "foreign-key column"'
    ]


def _retired_messages(path: Path) -> list[tuple[int, str]]:
    return [
        (violation.line, violation.message) for violation in retired_vocabulary(path)
    ]


FOREIGN_KEY_RETIRED = (
    '"ForeignKey(" is retired; write "Field(foreign_key=...) and Forward("<column>")"'
)
RELATED_NAME_RETIRED = (
    '"related_name=" is retired; write "the column name in Forward, Reverse or '
    'ManyToMany(through=...)"'
)
BACKREF_RETIRED = '"BackRef(" is retired; write "Reverse("<column>")"'


def test_a_fence_declaring_the_old_forward_relation_names_both_terms(
    tmp_path: Path,
) -> None:
    page = _write(
        tmp_path,
        "page.md",
        """
        # Page

        ```python
        class Post(Model):
            author: Annotated[Author, ForeignKey(related_name="posts")]
        ```
        """,
    )
    assert _retired_messages(page) == [
        (5, FOREIGN_KEY_RETIRED),
        (5, RELATED_NAME_RETIRED),
    ]


def test_a_docstring_naming_a_populated_relation_fails_at_its_line(
    tmp_path: Path,
) -> None:
    module = _write(
        tmp_path,
        "module.py",
        '''
        import ferro


        def team_of(player):
            """Return the team.

            The team is a populated relation after include().
            """
            populated = player.team  # code, never prose
            return populated
        ''',
    )
    assert _retired_messages(module) == [
        (7, '"populated relation" is retired; write "loaded relation"')
    ]


def test_inline_code_in_prose_is_matched(tmp_path: Path) -> None:
    page = _write(
        tmp_path,
        "page.md",
        """
        # Page

        Write `ManyToMany(related_name="players")` on one side.
        """,
    )
    assert _retired_messages(page) == [(3, RELATED_NAME_RETIRED)]


ALL_FIVE_TERMS = """
# Page

`ForeignKey(related_name="posts")` and `BackRef()` made the shadow column
and the populated relation.

```python
author: Annotated[Author, ForeignKey(related_name="posts")]
posts: Relation[list["Post"]] = BackRef()
```
"""


def test_the_upgrading_guide_and_the_changelog_are_exempt(tmp_path: Path) -> None:
    for name in ("upgrading.md", "changelog.md"):
        record = _write(tmp_path, name, ALL_FIVE_TERMS)
        assert (_lint(record).returncode, _lint(record).stdout) == (0, "")
    assert len(retired_vocabulary(_write(tmp_path, "page.md", ALL_FIVE_TERMS))) == 8


def test_another_librarys_foreign_key_is_legal(tmp_path: Path) -> None:
    page = _write(
        tmp_path,
        "page.md",
        """
        # Page

        ```python
        from sqlalchemy import ForeignKey

        author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
        ```
        """,
    )
    assert (_lint(page).returncode, _lint(page).stdout) == (0, "")


def test_a_string_literal_in_code_is_never_matched(tmp_path: Path) -> None:
    module = _write(
        tmp_path,
        "module.py",
        """
        def __getattr__(name):
            raise ImportError("ferro.ForeignKey is gone: write ForeignKey( no more")
        """,
    )
    assert (_lint(module).returncode, _lint(module).stdout) == (0, "")


def test_foreign_key_violation_error_is_not_a_retired_term(tmp_path: Path) -> None:
    page = _write(
        tmp_path,
        "page.md",
        """
        # Page

        The delete raises `ForeignKeyViolationError` while children exist.
        """,
    )
    assert (_lint(page).returncode, _lint(page).stdout) == (0, "")


def test_a_docstring_fence_is_reported_once(tmp_path: Path) -> None:
    module = _write(
        tmp_path,
        "module.py",
        '''
        def helper():
            """Return the posts.

            Examples:
                ```python
                posts: Relation[list["Post"]] = BackRef()
                ```
            """
        ''',
    )
    assert _retired_messages(module) == [(6, BACKREF_RETIRED)]


def test_a_comment_in_code_is_prose(tmp_path: Path) -> None:
    page = _write(
        tmp_path,
        "page.md",
        """
        # Page

        ```python
        team_id: int  # the shadow column
        ```
        """,
    )
    module = _write(
        tmp_path,
        "module.py",
        '''
        def helper():
            """Return the posts.

            ```python
            posts = author.posts  # a populated relation
            ```
            """
            return author.team  # read the BackRef( side
        ''',
    )
    assert _retired_messages(page) == [
        (4, '"shadow column" is retired; write "foreign-key column"')
    ]
    assert _retired_messages(module) == [
        (5, '"populated relation" is retired; write "loaded relation"'),
        (8, BACKREF_RETIRED),
    ]
