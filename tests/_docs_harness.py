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

``lint=check`` also lints the block (docs pages only). Every Python fence on
a docs page carries a ``test`` mode (``page_fence_mode``); a docstring fence
without one is compiled. Any other setting, or a mode without its setting, is
refused with the marker to write.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import griffe
from pytest_examples import CodeExample
from pytest_examples.find_examples import remove_indent

REPO_ROOT = Path(__file__).resolve().parents[1]
PRELUDES = REPO_ROOT / "docs" / "examples" / "_preludes"

_SETTING = re.compile(r"""([A-Za-z_][\w-]*)=(?:"([^"]*)"|'([^']*)'|([^\s{}"']+))""")
_FENCE = re.compile(r"(^ *```)( *)(.*?)\n(.+?)\1", flags=re.M | re.S)


_TEST_MODES = ("run", "compile", "pytest")
# The setting each test mode needs, and the only mode that takes it.
_MODE_SETTING = {"run": "prelude", "compile": "reason", "pytest": "path"}
_KNOWN_SETTINGS = frozenset({"test", "lint", *_MODE_SETTING.values()})
_MODE_EXAMPLE = {
    "run": "prelude=<name>",
    "compile": 'reason="<why it cannot run>"',
    "pytest": 'path="tests/<file>.py"',
}


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
    return settings


def page_fence_mode(example: CodeExample) -> str:
    """Return how a docs page's Python fence is tested: run, compile or pytest.

    A page fence must say: one that runs is marked
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


def fenced_examples(
    path: Path, text: str, *, line_offset: int = 0
) -> list[CodeExample]:
    """Find the Python fences in ``text``, numbered as lines of ``path``.

    ``line_offset`` is the number of lines of ``path`` before ``text``. Each
    example's ``start_line`` is its opening fence's line, as pytest-examples
    numbers them, so its source starts on the next line.
    """
    examples = []
    for match in _FENCE.finditer(text):
        opening, space, prefix, source = match.groups()
        prefix = prefix.lower()
        if not prefix.startswith(("py", "{.py")):
            continue
        start_line = line_offset + text[: match.start()].count("\n") + 1
        dedented, indent = remove_indent(source)
        start_index = match.start() + len(opening) + len(space) + len(prefix) + 1
        examples.append(
            CodeExample(
                source=dedented,
                path=path,
                start_line=start_line,
                end_line=start_line + source.count("\n") + 1,
                start_index=start_index,
                end_index=start_index + len(source),
                prefix=prefix,
                indent=indent,
            )
        )
    return examples


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
    root = griffe.load(package, search_paths=[str(search_path)])
    examples: list[CodeExample] = []
    seen: set[tuple[Path, int]] = set()
    for obj in _documented(root):
        docstring = obj.docstring
        if docstring is None or docstring.lineno is None:
            continue
        path = Path(obj.filepath) if not isinstance(obj.filepath, list) else None
        if path is None or (path, docstring.lineno) in seen:
            continue
        seen.add((path, docstring.lineno))
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        text = "".join(lines[docstring.lineno - 1 : docstring.endlineno])
        examples += fenced_examples(path, text, line_offset=docstring.lineno - 1)
    return sorted(examples, key=lambda example: (str(example.path), example.start_line))


def run_fence(example: CodeExample) -> str:
    """Run a ``{test=run prelude=<name>}`` example and return what it printed.

    The example runs in a subprocess, so models and engines never leak
    between examples. A failure raises ``AssertionError`` carrying the
    traceback, whose frames name the example's own file and line.
    """
    settings = fence_settings(example)
    if settings.get("test") != "run":
        raise ValueError(
            f"{example}: only a fence marked {{test=run prelude=<name>}} runs"
        )
    prelude = settings["prelude"]
    result = subprocess.run(
        [
            sys.executable,
            str(PRELUDES / "__init__.py"),
            prelude,
            str(example.path),
            str(example.start_line + 1),
        ],
        input=example.source,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"{example} failed after prelude {prelude!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout
