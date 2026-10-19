"""Shared pieces of the docs examples harness.

A Python fence in a docs page or a docstring can carry settings after its
language:

    ```python {test=run prelude=reference}
    print(await Author.where(lambda author: author.id == 1).first())
    ```

``test=run`` runs the block. With ``prelude=<name>`` it runs in a fresh
interpreter after ``docs/examples/_preludes/<name>.py``, which declares the
models, connects and opens a session; top-level ``await`` works there.
``lint=check`` lints the block (docs pages only).
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


def fence_settings(example: CodeExample) -> dict[str, str]:
    """Read the ``key=value`` settings after a fence's language.

    Values may be bare (``test=run``) or quoted (``lint="check"``). A
    ``prelude`` without ``test=run`` is refused: it would silently do nothing.
    """
    settings = {
        match.group(1): next(g for g in match.groups()[1:] if g is not None)
        for match in _SETTING.finditer(example.prefix)
    }
    if "prelude" in settings and settings.get("test") != "run":
        raise ValueError(
            f"{example}: prelude={settings['prelude']} needs test=run; "
            f"mark the fence {{test=run prelude={settings['prelude']}}}"
        )
    return settings


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
    prelude = fence_settings(example)["prelude"]
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
