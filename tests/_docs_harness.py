"""Shared pieces of the docs examples harness.

A Python fence in a docs page or a docstring can carry settings after its
language:

    ```python {test=run prelude=reference}
    print(await Author.where(lambda author: author.id == 1).first())
    ```

``test`` says how the block is tested, and each mode takes one setting:

- ``{test=run prelude=<name>}`` runs the block in a fresh interpreter after
  ``docs/examples/_preludes/<name>.py``, which declares the models, connects
  and opens a session; top-level ``await`` works there.
- ``{test=compile reason="..."}`` only compiles the block and says why it
  cannot run (it needs Postgres, or it is a generated file).
- ``{test=pytest path="tests/<file>.py"}`` is a test module the testing
  how-to hands readers; ``tests/test_docs_testing_fixtures.py`` writes it at
  that path in a scratch project and runs pytest there.

``backend=postgres`` on a ``test=run`` fence runs it against PostgreSQL:
every ``connect()`` the prelude or the block makes is pointed at the test
database, whatever URL it names. Such a fence runs only under the
``postgres_only`` marker, so a run without Postgres skips it, saying so.

``lint=check`` also lints the block. Every Python fence, on a docs page or in
a docstring, carries a ``test`` mode (``fence_mode``). Any other setting, or a
mode without its setting, is refused with the marker to write.

One reader finds every fenced block (``fenced_blocks``), and one runner starts
every subprocess the docs tests start (``run_python``).
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import griffe
from pytest_examples import CodeExample, EvalExample
from pytest_examples.find_examples import remove_indent

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_PAGES = REPO_ROOT / "docs" / "pages"
PRELUDES = REPO_ROOT / "docs" / "examples" / "_preludes"

_SETTING = re.compile(r"""([A-Za-z_][\w-]*)=(?:"([^"]*)"|'([^']*)'|([^\s{}"']+))""")


_TEST_MODES = ("run", "compile", "pytest")
# The setting each test mode needs, and the only mode that takes it.
_MODE_SETTING = {"run": "prelude", "compile": "reason", "pytest": "path"}
_KNOWN_SETTINGS = frozenset({"test", "lint", "backend", *_MODE_SETTING.values()})
_BACKENDS = ("postgres",)
"""The databases a ``test=run`` fence can ask for besides SQLite."""
_MODE_EXAMPLE = {
    "run": "prelude=<name>",
    "compile": 'reason="<why it cannot run>"',
    "pytest": 'path="tests/<file>.py"',
}


# -- the one runner ----------------------------------------------------------------


def run_python(
    args: Sequence[str],
    *,
    timeout: float = 180,
    env: dict[str, str] | None = None,
    cwd: Path = REPO_ROOT,
    input: str | None = None,  # noqa: A002 - subprocess.run's own name
    clean_env: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run this interpreter with ``args`` and return the finished process.

    Args:
        args: The arguments after the interpreter (a script and its arguments,
            ``-m module`` or ``-c code``).
        timeout: Seconds before the run is killed. Defaults to 180.
        env: Variables to set for the run, over this process's environment.
        cwd: The working directory. Defaults to the repository root.
        input: Text piped to the run's stdin.
        clean_env: Start from an empty environment instead of this process's,
            so only ``env`` reaches the run.

    Returns:
        The process, its ``stdout`` and ``stderr`` as text.

    Raises:
        AssertionError: The run exited non-zero. The message names the command,
            the exit code and both streams.
    """
    command = [sys.executable, *args]
    result = subprocess.run(
        command,
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env={**({} if clean_env else os.environ), **(env or {})},
    )
    if result.returncode != 0:
        raise AssertionError(
            f"{' '.join(command)} exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


# -- the one fence reader ----------------------------------------------------------


@dataclass(frozen=True)
class Fence:
    """One fenced block of a Markdown file or a docstring."""

    path: Path
    """The file the block is in."""
    line: int
    """The line of the block's opening fence."""
    language: str
    """The first word of the info string (``python``, ``sql``, ``text``), or ``""``."""
    info: str
    """Everything after the opening backticks, stripped."""
    body: str
    """Everything between the fences, the fence's own indentation removed."""
    indent: str
    """The indentation of the opening fence."""
    note: tuple[str, str] | None
    """The ``<!-- <kind>: <value> -->`` comment on the line above the fence, at
    the same indentation, as ``(kind, value)``."""

    @property
    def where(self) -> str:
        """The block's file and opening line, as ``<path>:<line>``."""
        return f"{self.path}:{self.line}"


_OPENING = re.compile(r"^(?P<indent>[ \t]*)(?P<ticks>`{3,}|~{3,})(?P<info>[^`\n]*)$")
_NOTE = re.compile(r"^<!-- (?P<kind>[a-z][a-z-]*): (?P<value>.+?) -->$")


def _language(info: str) -> str:
    word = info.lstrip("{").split(" ", 1)[0].split("{", 1)[0]
    return word.lstrip(".").lower()


def fenced_blocks(
    path: Path, text: str, language: str | None = None, *, line_offset: int = 0
) -> list[Fence]:
    """Read every fenced block of ``text``, numbered as lines of ``path``.

    A fence opens with three or more backticks (or tildes) and closes on a line
    holding only the same run at the same indentation, so a longer fence can
    quote a shorter one. Each block's ``note`` is the HTML comment directly
    above it, the way pages mark where a block comes from
    (``<!-- prints: predicates.py:first-query -->``).

    Args:
        path: The file ``text`` comes from, named in each block and error.
        text: The Markdown, or a docstring's lines as they sit in the file.
        language: Keep only the blocks in this language. Defaults to every block.
        line_offset: The number of lines of ``path`` before ``text``.

    Returns:
        The blocks in file order.

    Raises:
        ValueError: A fence is never closed; the message names its line.
    """
    lines = text.splitlines(keepends=True)
    blocks: list[Fence] = []
    index = 0
    while index < len(lines):
        opening = _OPENING.match(lines[index].rstrip("\n"))
        if opening is None:
            index += 1
            continue
        indent, ticks = opening["indent"], opening["ticks"]
        info = opening["info"].strip()
        closing = re.compile(
            rf"^{re.escape(indent)}{re.escape(ticks[0])}{{{len(ticks)},}}[ \t]*$"
        )
        end = next(
            (n for n in range(index + 1, len(lines)) if closing.match(lines[n])),
            None,
        )
        if end is None:
            raise ValueError(
                f"{path}:{line_offset + index + 1}: the {ticks}{_language(info)} "
                "fence is never closed"
            )
        note = None
        if index > 0:
            above = lines[index - 1].rstrip("\n")
            if above.startswith(indent):
                marked = _NOTE.match(above[len(indent) :])
                if marked is not None:
                    note = (marked["kind"], marked["value"])
        body = "".join(line.removeprefix(indent) for line in lines[index + 1 : end])
        fence = Fence(
            path, line_offset + index + 1, _language(info), info, body, indent, note
        )
        if language is None or fence.language == language:
            blocks.append(fence)
        index = end + 1
    return blocks


def page_fences(language: str | None = None) -> list[Fence]:
    """Read every fenced block a docs page shows, numbered as lines of its file.

    A page's own blocks come first, then the blocks of every Markdown section a
    page pulls in with ``--8<-- "<file>.md:<section>"`` (``CONTRIBUTING.md``,
    ``README.md``, ``docs/includes``), each named by its own file and line, so
    a block reaches the reader only through a reader that sees it.
    """
    blocks: list[Fence] = []
    included: set[tuple[Path, str]] = set()
    for page in sorted(DOCS_PAGES.rglob("*.md")):
        text = page.read_text(encoding="utf-8")
        blocks += fenced_blocks(page, text, language)
        for match in _INCLUDE.finditer(text):
            target = REPO_ROOT / match["file"]
            if target.name == "CHANGELOG.md":
                continue  # Release tooling writes it; it holds no examples.
            included.add((target, match["section"] or ""))
    for target, section in sorted(included):
        text, offset = _section(target, section)
        blocks += fenced_blocks(target, text, language, line_offset=offset)
    return blocks


_INCLUDE = re.compile(r'--8<-- "(?P<file>[^":]+\.md)(?::(?P<section>[A-Za-z_-]+))?"')


def _section(path: Path, section: str) -> tuple[str, int]:
    """The text of ``path`` between its ``[start:<section>]`` and
    ``[end:<section>]`` lines, and the number of lines before it."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if not section:
        return "".join(lines), 0
    start = next(
        (n for n, line in enumerate(lines) if f"[start:{section}]" in line), None
    )
    end = next((n for n, line in enumerate(lines) if f"[end:{section}]" in line), None)
    if start is None or end is None:
        raise ValueError(f"{path} has no section {section!r}")
    return "".join(lines[start + 1 : end]), start + 1


def fenced_examples(
    path: Path, text: str, *, line_offset: int = 0
) -> list[CodeExample]:
    """Find the Python fences in ``text``, numbered as lines of ``path``.

    ``line_offset`` is the number of lines of ``path`` before ``text``. Each
    example's ``start_line`` is its opening fence's line, as pytest-examples
    numbers them, so its source starts on the next line.
    """
    return [
        _example(fence)
        for fence in fenced_blocks(path, text, line_offset=line_offset)
        if fence.language in ("py", "python")
    ]


def _example(fence: Fence) -> CodeExample:
    source, indent = remove_indent(fence.body)
    return CodeExample(
        source=source,
        path=fence.path,
        start_line=fence.line,
        end_line=fence.line + fence.body.count("\n") + 1,
        start_index=0,
        end_index=0,
        prefix=fence.info.lower(),
        indent=len(fence.indent) + indent,
    )


def page_examples() -> list[CodeExample]:
    """Every Python fence a docs page shows (``page_fences``), as an example.

    A fence holding a ``--8<--`` line embeds an example script, which
    ``tests/test_docs_examples.py`` runs as a whole, and is left out.
    """
    return [
        _example(fence)
        for fence in page_fences()
        if fence.language in ("py", "python") and "--8<--" not in fence.body
    ]


# -- fence settings ---------------------------------------------------------------


def preludes() -> list[str]:
    """Name every prelude a fence can ask for."""
    return sorted(p.stem for p in PRELUDES.glob("*.py") if not p.stem.startswith("_"))


def fence_settings(example: CodeExample) -> dict[str, str]:
    """Read the ``key=value`` settings after a fence's language.

    Values may be bare (``test=run``) or quoted (``lint="check"``). Any
    setting the harness would silently ignore is refused, with the marker to
    write instead: an unknown key (such as ``compile-only=``), a ``test``
    mode other than ``run``, ``compile`` or ``pytest``, a mode without the
    setting it needs, and ``prelude``, ``reason`` or ``path`` without its mode.
    """
    settings = {
        match.group(1): next(g for g in match.groups()[1:] if g is not None)
        for match in _SETTING.finditer(example.prefix)
    }
    unknown = sorted(set(settings) - _KNOWN_SETTINGS)
    if unknown:
        raise ValueError(
            f"{example}: unknown setting {', '.join(unknown)}; a block that runs "
            "is marked {test=run prelude=<name>}, one that cannot says why with "
            '{test=compile reason="..."}'
        )
    mode = settings.get("test")
    if mode is not None and mode not in _TEST_MODES:
        raise ValueError(
            f"{example}: test={mode} is not run, compile or pytest; mark the fence "
            '{test=run prelude=<name>} or {test=compile reason="..."}'
        )
    for needed_by, setting in _MODE_SETTING.items():
        if mode == needed_by and not settings.get(setting):
            hint = _MODE_EXAMPLE[needed_by]
            if needed_by == "run":
                hint += f" (one of: {', '.join(preludes())})"
            raise ValueError(f"{example}: test={needed_by} needs {hint}")
        if setting in settings and mode != needed_by:
            raise ValueError(
                f"{example}: {setting}={settings[setting]} needs test={needed_by}; "
                f"mark the fence "
                f"{{test={needed_by} {setting}={settings[setting]}}}"
            )
    if "lint" in settings and settings["lint"] != "check":
        raise ValueError(f"{example}: lint={settings['lint']} is not lint=check")
    backend = settings.get("backend")
    if backend is not None and backend not in _BACKENDS:
        raise ValueError(f"{example}: backend={backend} is not backend=postgres")
    if backend is not None and mode != "run":
        raise ValueError(
            f"{example}: backend={backend} needs test=run; mark the fence "
            f"{{test=run prelude=<name> backend={backend}}}"
        )
    return settings


def fence_mode(example: CodeExample) -> str:
    """Return how a Python fence is tested: run, compile or pytest.

    A fence on a docs page or in a docstring must say: one that runs is marked
    ``{test=run prelude=<name>}``, one that cannot run says why with
    ``{test=compile reason="..."}``. An unmarked fence is refused, so no block
    is compile-only by accident.
    """
    mode = fence_settings(example).get("test")
    if mode is None:
        raise ValueError(
            f"{example}: the fence says neither test=run nor test=compile; mark it "
            '{test=run prelude=<name>}, or {test=compile reason="<why it cannot '
            'run>"} when it cannot run'
        )
    return mode


_LINT_IGNORES = ["D", "T", "B", "C4", "E711", "E712", "E721"]
"""Ruff rules a docs block may break: blocks print, and show partial code."""


def check_fence(
    example: CodeExample,
    *,
    eval_example: EvalExample | None = None,
    database_url: str | None = None,
) -> str | None:
    """Hold one Python fence to its contract and return what it printed.

    The fence compiles (top-level ``await`` allowed), says how it is tested
    (``fence_mode``), is linted when marked ``lint=check``, and runs when
    marked ``test=run``. A ``test=pytest`` fence is run by the testing
    how-to's own test. A ``backend=postgres`` fence runs only when given
    ``database_url``.

    Args:
        example: The fence, from a docs page or a docstring.
        eval_example: pytest-examples' fixture, which lints a ``lint=check``
            fence. A fence marked ``lint=check`` without one is refused.
        database_url: The PostgreSQL database a ``backend=postgres`` fence runs
            against. Without it such a fence is only compiled and checked.

    Returns:
        What a ``test=run`` fence printed, or ``None`` for one that only
        compiles.

    Raises:
        ValueError: The fence is unmarked or mis-marked.
        AssertionError: The fence failed to run.
    """
    compile(
        example.source, str(example.path), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
    )
    mode = fence_mode(example)
    if fence_settings(example).get("lint") == "check":
        if eval_example is None:
            raise ValueError(f"{example}: lint=check needs the eval_example fixture")
        eval_example.set_config(
            ruff_ignore=_LINT_IGNORES, line_length=100, target_version="py313"
        )
        eval_example.lint(example)
    if mode != "run":
        return None
    if fence_settings(example).get("backend") == "postgres" and database_url is None:
        return None
    return run_fence(example, database_url=database_url)


# -- docstrings -------------------------------------------------------------------


def _documented(obj: griffe.Object) -> Iterator[griffe.Object]:
    yield obj
    for member in obj.members.values():
        if not member.is_alias:
            yield from _documented(member)


def docstring_examples(package: str, search_path: Path) -> list[CodeExample]:
    """Collect the Python fences in the docstrings of every module of ``package``.

    griffe finds each docstring and its lines; the fences are read from the
    file's own lines, so every example carries its real file and line.
    """
    # Static only: once ``ferro`` is imported, griffe would otherwise inspect
    # the compiled ``_core`` module and hand back its ``.so`` as a source file.
    root = griffe.load(package, search_paths=[str(search_path)], allow_inspection=False)
    examples: list[CodeExample] = []
    seen: set[tuple[Path, int]] = set()
    for obj in _documented(root):
        docstring = obj.docstring
        if docstring is None or docstring.lineno is None:
            continue
        path = Path(obj.filepath) if not isinstance(obj.filepath, list) else None
        if path is None or path.suffix not in {".py", ".pyi"}:
            continue
        if (path, docstring.lineno) in seen:
            continue
        seen.add((path, docstring.lineno))
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        text = "".join(lines[docstring.lineno - 1 : docstring.endlineno])
        examples += fenced_examples(path, text, line_offset=docstring.lineno - 1)
    return sorted(examples, key=lambda example: (str(example.path), example.start_line))


def run_fence(example: CodeExample, *, database_url: str | None = None) -> str:
    """Run a ``{test=run prelude=<name>}`` example and return what it printed.

    The example runs in a subprocess, so models and engines never leak
    between examples. A failure raises ``AssertionError`` carrying the
    traceback, whose frames name the example's own file and line.

    Args:
        example: The fence.
        database_url: The PostgreSQL database a ``backend=postgres`` fence runs
            against; every ``connect()`` in the run is pointed at it.

    Raises:
        ValueError: The fence is not marked ``test=run``, or is marked
            ``backend=postgres`` and no ``database_url`` was given.
    """
    settings = fence_settings(example)
    if settings.get("test") != "run":
        raise ValueError(
            f"{example}: only a fence marked {{test=run prelude=<name>}} runs"
        )
    prelude = settings["prelude"]
    env = None
    if settings.get("backend") == "postgres":
        if database_url is None:
            raise ValueError(
                f"{example}: backend=postgres needs a database_url; it runs "
                "under the postgres_only marker"
            )
        env = {"FERRO_DOCS_DATABASE_URL": database_url}
    try:
        result = run_python(
            [
                str(PRELUDES / "__init__.py"),
                prelude,
                str(example.path),
                str(example.start_line + 1),
            ],
            input=example.source,
            timeout=120,
            env=env,
        )
    except AssertionError as failure:
        raise AssertionError(
            f"{example} failed after prelude {prelude!r}\n{failure}"
        ) from None
    return result.stdout
