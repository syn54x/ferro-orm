from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

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

    settings = example.prefix_settings()

    # Opt in for stricter linting using pytest-examples prefix settings:
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

    # Opt in for runnable snippets using pytest-examples prefix settings:
    # ```python {test=run}
    # ...
    # ```
    if settings.get("test") == "run":
        eval_example.run_print_check(example)
        eval_example.run(example)


@pytest.mark.parametrize(
    "script",
    sorted(DOCS_EXAMPLES.glob("*.py")),
    ids=lambda p: p.name,
)
def test_docs_example_scripts(script: Path) -> None:
    """Every docs example script must run end to end.

    Each script runs in a subprocess so model registries and engine state
    never leak between examples (or into other tests).
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
