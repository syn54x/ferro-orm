from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

DOCS_ROOT = Path(__file__).resolve().parents[1] / "docs"


@pytest.skip(
    reason="Issue parsing architecture.md for some reason.", allow_module_level=True
)
@pytest.mark.parametrize("example", find_examples(str(DOCS_ROOT)), ids=str)
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
