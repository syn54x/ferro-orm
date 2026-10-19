from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

from tests._docs_harness import fence_settings, run_fence

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_PAGES = REPO_ROOT / "docs" / "pages"
DOCS_EXAMPLES = REPO_ROOT / "docs" / "examples"


def _inline_examples() -> list[CodeExample]:
    # Snippet directives (--8<--) are expanded by the docs build, not by
    # pytest-examples, so blocks containing them are not valid Python here.
    return [
        example
        for example in find_examples(str(DOCS_PAGES))
        if "--8<--" not in example.source
    ]


@pytest.mark.parametrize("example", _inline_examples(), ids=str)
def test_docs_examples(example: CodeExample, eval_example: EvalExample) -> None:
    """Validate docs snippets, with opt-in linting/execution."""
    # Baseline for all snippets: parse + compile as valid Python syntax.
    compile(
        example.source,
        str(example.path),
        "exec",
        flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
    )

    settings = fence_settings(example)

    # Opt in for stricter linting:
    # ```python {lint=check}
    # ...
    # ```
    if settings.get("lint") == "check":
        eval_example.set_config(
            # Docs snippets often intentionally use print statements or partial examples.
            ruff_ignore=["D", "T", "B", "C4", "E721"],
            line_length=100,
            target_version="py313",
        )
        eval_example.lint(example)

    # Opt in for runnable snippets:
    # ```python {test=run}
    # ...
    # ```
    # With a prelude, the block runs in a subprocess after
    # docs/examples/_preludes/<name>.py, inside its session:
    # ```python {test=run prelude=reference}
    # ...
    # ```
    if settings.get("test") == "run":
        if "prelude" in settings:
            run_fence(example)
        else:
            eval_example.run_print_check(example)
            eval_example.run(example)


@pytest.mark.parametrize(
    "script",
    sorted(
        path for path in DOCS_EXAMPLES.glob("*.py") if not path.name.startswith("_")
    ),
    ids=lambda p: p.name,
)
def test_docs_example_scripts(script: Path) -> None:
    """Every docs example script must run end to end.

    Each script runs in a subprocess so model registries and engine state
    never leak between examples (or into other tests). A module whose name
    starts with ``_`` is a helper the examples import (the migrations
    examples' ``_migrations_project``), not an example. The migrations
    examples build their project in a temporary directory and drive
    ``ferro migrate`` against SQLite there.
    """
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"{script.name} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _page(tmp_path: Path, prefix: str, body: str) -> CodeExample:
    page = tmp_path / "page.md"
    page.write_text(f"# Page\n\n```{prefix}\n{body}\n```\n", encoding="utf-8")
    [example] = find_examples(str(page))
    return example


def test_prelude_fence_runs_after_its_prelude(tmp_path: Path) -> None:
    example = _page(
        tmp_path,
        "python {test=run prelude=reference}",
        "print(await Author.where(lambda author: author.id == 1).first())",
    )
    assert fence_settings(example) == {"test": "run", "prelude": "reference"}
    output = run_fence(example)
    assert "Ada Lovelace" in output


def test_prelude_fence_failure_names_the_page_line(tmp_path: Path) -> None:
    example = _page(
        tmp_path,
        "python {test=run prelude=reference}",
        'author = await Author.get(1)\nraise RuntimeError("boom")',
    )
    with pytest.raises(AssertionError) as failure:
        run_fence(example)
    message = str(failure.value)
    assert f'File "{example.path}", line 5' in message
    assert "RuntimeError: boom" in message


def test_unknown_prelude_fails_loudly(tmp_path: Path) -> None:
    example = _page(tmp_path, "python {test=run prelude=nope}", "pass")
    with pytest.raises(AssertionError, match="no prelude named 'nope'"):
        run_fence(example)


def test_prelude_without_test_run_is_refused(tmp_path: Path) -> None:
    example = _page(tmp_path, "python {prelude=reference}", "pass")
    with pytest.raises(ValueError, match=r"prelude=reference needs test=run"):
        fence_settings(example)


def test_fence_settings_read_bare_and_quoted_values() -> None:
    example = CodeExample.create(
        "pass", prefix='python {test=run lint="check"}', path=Path("p.md")
    )
    assert fence_settings(example) == {"test": "run", "lint": "check"}
