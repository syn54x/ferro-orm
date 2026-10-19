from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

from tests._docs_harness import fence_settings, page_fence_mode, run_fence

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
    """Every inline page block compiles; one marked to run, runs.

    Each block says how it is tested (``tests._docs_harness.page_fence_mode``):
    ``{test=run prelude=<name>}`` runs it after that prelude,
    ``{test=compile reason="..."}`` compiles it and says why it cannot run,
    ``{test=pytest path="tests/<file>.py"}`` runs it as a test module
    (``tests/test_docs_testing_fixtures.py``). ``lint=check`` also lints it.
    """
    compile(
        example.source,
        str(example.path),
        "exec",
        flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
    )
    mode = page_fence_mode(example)
    if fence_settings(example).get("lint") == "check":
        eval_example.set_config(
            # Docs snippets often intentionally use print statements or partial examples.
            ruff_ignore=["D", "T", "B", "C4", "E711", "E712", "E721"],
            line_length=100,
            target_version="py313",
        )
        eval_example.lint(example)
    if mode == "run":
        run_fence(example)


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


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    "script", ["row_level_security.py", "row_level_security_annotated.py"]
)
def test_row_level_security_example_runs_on_postgres(
    script: str, db_url: str, pg_role
) -> None:
    """The row-level security pages' demo, on Postgres.

    The script migrates as the test's (superuser) role, creates the
    application's NOSUPERUSER login role and asserts what each session sees:
    a tenant-scoped session reads its own two invoices, a session without
    settings reads none, the migrating role reads all four, and a write into
    another tenant is refused. Without arguments the script runs on SQLite
    (``test_docs_example_scripts``), where no policy exists to check.
    """
    result = subprocess.run(
        [sys.executable, str(DOCS_EXAMPLES / script), db_url, pg_role("app")],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"{script} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
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
        "pass",
        prefix='python {test=run prelude=reference lint="check"}',
        path=Path("p.md"),
    )
    assert fence_settings(example) == {
        "test": "run",
        "prelude": "reference",
        "lint": "check",
    }


def _fence(prefix: str) -> CodeExample:
    return CodeExample.create("pass", prefix=prefix, path=Path("page.md"))


def test_run_without_prelude_names_the_preludes() -> None:
    with pytest.raises(ValueError, match=r"test=run needs prelude=<name>.*reference"):
        fence_settings(_fence("python {test=run}"))


def test_compile_without_reason_is_refused() -> None:
    with pytest.raises(ValueError, match=r'test=compile needs reason="'):
        fence_settings(_fence("python {test=compile}"))


def test_pytest_without_path_is_refused() -> None:
    with pytest.raises(ValueError, match=r'test=pytest needs path="tests/'):
        fence_settings(_fence("python {test=pytest}"))


def test_unknown_test_mode_is_refused() -> None:
    with pytest.raises(ValueError, match=r"test=skip is not run, compile or pytest"):
        fence_settings(_fence("python {test=skip}"))


def test_other_compile_only_spelling_is_refused() -> None:
    with pytest.raises(
        ValueError, match=r'unknown setting compile-only.*test=compile reason="'
    ):
        fence_settings(_fence('python {compile-only="needs Postgres"}'))


def test_reason_without_compile_is_refused() -> None:
    with pytest.raises(ValueError, match=r"reason=x needs test=compile"):
        fence_settings(_fence('python {test=run prelude=reference reason="x"}'))


def test_unmarked_page_fence_must_say_how_it_is_tested() -> None:
    with pytest.raises(
        ValueError, match=r'says neither test=run.*test=compile reason="'
    ):
        page_fence_mode(_fence("python"))


def test_compile_only_page_fence_states_its_reason() -> None:
    assert (
        page_fence_mode(_fence('python {test=compile reason="needs Postgres"}'))
        == "compile"
    )


README = REPO_ROOT / "README.md"
_README_FENCE = re.compile(r"^```python\n(.*?)^```", flags=re.M | re.S)
_MODELS_SECTION = re.compile(
    r"# --8<-- \[start:models\]\n(.*?)# --8<-- \[end:models\]", flags=re.S
)


def _readme_quick_start() -> str:
    [source] = _README_FENCE.findall(README.read_text(encoding="utf-8"))
    return source


def test_readme_quick_start_runs_and_prints_what_it_claims(tmp_path: Path) -> None:
    """The README's Python block runs as written and prints its stated output."""
    script = tmp_path / "readme_quick_start.py"
    script.write_text(_readme_quick_start(), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "Found 1 active users." in README.read_text(encoding="utf-8")
    assert result.stdout.strip() == "Found 1 active users."


def test_readme_model_is_the_home_page_example() -> None:
    """The README's model is the one the home page embeds in both tabs."""
    [models] = _MODELS_SECTION.findall(
        (DOCS_EXAMPLES / "readme_quickstart.py").read_text(encoding="utf-8")
    )
    assert models.strip() in _readme_quick_start()
