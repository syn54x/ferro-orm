"""Docstring examples in ``src/ferro``: every one compiles, marked ones run.

A fenced Python example in a docstring is compiled. One marked
``{test=run prelude=<name>}`` also runs in a subprocess after
``docs/examples/_preludes/<name>.py``, inside its session, the way a marked
fence on a docs page does.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest
from pytest_examples import CodeExample

from tests._docs_harness import docstring_examples, fence_settings, run_fence

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "example", docstring_examples("ferro", REPO_ROOT / "src"), ids=str
)
def test_docstring_example(example: CodeExample) -> None:
    compile(
        example.source,
        str(example.path),
        "exec",
        flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
    )
    if fence_settings(example).get("test") == "run":
        run_fence(example)


def _scratch_package(tmp_path: Path, body: str) -> Path:
    package = tmp_path / "scratch"
    package.mkdir()
    (package / "__init__.py").write_text('"""Scratch package."""\n', "utf-8")
    (package / "library.py").write_text(
        textwrap.dedent(body).lstrip("\n"), encoding="utf-8"
    )
    return package / "library.py"


def test_marked_docstring_example_is_collected_and_run(tmp_path: Path) -> None:
    module = _scratch_package(
        tmp_path,
        '''
        def first_author():
            """Return the first author.

            Examples:
                ```python {test=run prelude=reference}
                author = await Author.where(lambda author: author.id == 1).first()
                print(author.name)
                ```

                ```python
                first_author()
                ```
            """
        ''',
    )
    examples = docstring_examples("scratch", tmp_path)
    assert [(ex.path, ex.start_line) for ex in examples] == [
        (module, 5),
        (module, 10),
    ]
    marked, unmarked = examples
    assert fence_settings(marked) == {"test": "run", "prelude": "reference"}
    assert fence_settings(unmarked) == {}
    assert run_fence(marked).strip() == "Ada Lovelace"


def test_failing_docstring_example_names_its_file_line(tmp_path: Path) -> None:
    module = _scratch_package(
        tmp_path,
        '''
        class Library:
            """A library.

            ```python {test=run prelude=reference}
            raise RuntimeError("boom")
            ```
            """
        ''',
    )
    [example] = docstring_examples("scratch", tmp_path)
    with pytest.raises(AssertionError) as failure:
        run_fence(example)
    assert f'File "{module}", line 5' in str(failure.value)
