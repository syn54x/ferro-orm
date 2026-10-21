"""Docstring examples in ``src/ferro``: every one is marked, compiles, and runs
when marked to.

A fenced Python example in a docstring holds to the contract a docs page fence
does (``tests._docs_harness.check_fence``): it says how it is tested, compiles,
and one marked ``{test=run prelude=<name>}`` runs in a subprocess after
``docs/examples/_preludes/<name>.py``, inside its session. An unmarked fence is
refused. The entry points the reference leads with each keep an example that
runs (``RUNNING_ENTRY_POINTS``).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import griffe
import pytest
from pytest_examples import CodeExample

from tests._docs_harness import (
    check_fence,
    docstring_examples,
    fence_settings,
    run_fence,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "example", docstring_examples("ferro", REPO_ROOT / "src"), ids=str
)
def test_docstring_example(example: CodeExample) -> None:
    check_fence(example)


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    "example",
    [
        example
        for example in docstring_examples("ferro", REPO_ROOT / "src")
        if fence_settings(example).get("backend") == "postgres"
    ],
    ids=str,
)
def test_docstring_example_on_postgres(example: CodeExample, db_url: str) -> None:
    """A docstring fence marked ``backend=postgres`` runs against PostgreSQL."""
    check_fence(example, database_url=db_url)


RUNNING_ENTRY_POINTS = [
    "models.Model.create",
    "models.Model.get",
    "models.Model.where",
    "models.Model.save",
    "models.Model.delete",
    "query.builder.Query.after",
    "query.builder.Query.before",
    "query.builder.Query.position_of",
    "migrate",
]
"""The entry points whose docstrings must each keep an ``Examples:`` fence
marked ``{test=run prelude=<name>}``, as paths inside the ``ferro`` package."""


def entry_points_without_a_running_example(
    package: str, search_path: Path, paths: list[str]
) -> list[str]:
    """Name each of ``paths`` whose docstring holds no fence marked ``test=run``.

    Args:
        package: The package to load, such as ``ferro``.
        search_path: The directory that holds the package.
        paths: Objects inside the package, such as ``models.Model.create``.

    Returns:
        The paths with no running example, or with no docstring at all.
    """
    root = griffe.load(package, search_paths=[str(search_path)], allow_inspection=False)
    running: dict[Path, list[int]] = {}
    for example in docstring_examples(package, search_path):
        if fence_settings(example).get("test") == "run":
            running.setdefault(Path(example.path), []).append(example.start_line)
    missing = []
    for path in paths:
        obj = root[path]
        docstring = obj.docstring
        lines = running.get(Path(str(obj.filepath)), [])
        if docstring is None or not any(
            docstring.lineno <= line <= (docstring.endlineno or 0) for line in lines
        ):
            missing.append(path)
    return missing


def test_each_entry_point_keeps_a_running_example() -> None:
    assert (
        entry_points_without_a_running_example(
            "ferro", REPO_ROOT / "src", RUNNING_ENTRY_POINTS
        )
        == []
    )


def test_an_entry_point_whose_example_only_compiles_is_named(tmp_path: Path) -> None:
    _scratch_package(
        tmp_path,
        '''
        def first_author():
            """Return the first author.

            Examples:
                ```python {test=compile reason="shows a call"}
                first_author()
                ```
            """


        def last_author():
            """Return the last author.

            Examples:
                ```python {test=run prelude=reference}
                print(1)
                ```
            """


        def any_author():
            """Return an author."""
        ''',
    )
    assert entry_points_without_a_running_example(
        "scratch",
        tmp_path,
        ["library.first_author", "library.last_author", "library.any_author"],
    ) == ["library.first_author", "library.any_author"]


def test_an_unmarked_docstring_fence_is_refused(tmp_path: Path) -> None:
    _scratch_package(
        tmp_path,
        '''
        def first_author():
            """Return the first author.

            ```python
            first_author()
            ```
            """
        ''',
    )
    [example] = docstring_examples("scratch", tmp_path)
    with pytest.raises(ValueError, match=r"says neither test=run nor test=compile"):
        check_fence(example)


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


def test_docstring_example_can_declare_related_models(tmp_path: Path) -> None:
    _scratch_package(
        tmp_path,
        '''
        class Library:
            """A library.

            ```python {test=run prelude=reference}
            from typing import Annotated

            from ferro import BackRef, FerroField, ForeignKey, Model, Relation, create_tables


            class Shelf(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None
                label: str
                books: Relation[list["Book"]] = BackRef()


            class Book(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None
                title: str
                shelf: Annotated[Shelf, ForeignKey(related_name="books")]


            await create_tables()
            shelf = await Shelf.create(label="History")
            await Book.create(title="Middlemarch", shelf=shelf)
            print([book.title for book in await shelf.books.all()])
            ```
            """
        ''',
    )
    [example] = docstring_examples("scratch", tmp_path)
    assert run_fence(example).strip() == "['Middlemarch']"
