"""Preludes for runnable docs blocks, and the runner that uses them.

A prelude is a module in this directory. It declares the models a docs block
uses and defines ``opened()``, an async context manager that connects to an
in-memory SQLite database, adds a few rows and opens a session. A block marked
``{test=run prelude=<name>}`` sees every name the prelude defines and runs
inside ``opened()``, with top-level ``await``.

Run a block by piping its source to this file:

    python docs/examples/_preludes/__init__.py <prelude> <path> <first line>

``path`` and ``first line`` are where the block's source sits, so a traceback
points at the docs page or docstring, not at this runner. With
``FERRO_DOCS_DATABASE_URL`` set (a ``backend=postgres`` fence), every
``connect()`` the prelude or the block makes goes to that database instead of
the URL it names.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import os
import sys
from pathlib import Path
from types import ModuleType

PRELUDES = Path(__file__).resolve().parent


def load_prelude(name: str) -> ModuleType:
    """Import ``<name>.py`` from this directory, refusing a name that has none."""
    path = PRELUDES / f"{name}.py"
    if name.startswith("_") or not path.is_file():
        available = sorted(
            p.stem for p in PRELUDES.glob("*.py") if not p.stem.startswith("_")
        )
        raise SystemExit(
            f"no prelude named {name!r}; add docs/examples/_preludes/{name}.py "
            f"or use one of: {', '.join(available)}"
        )
    spec = importlib.util.spec_from_file_location(f"_ferro_prelude_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def run_block(prelude: str, source: str, path: str, first_line: int) -> None:
    """Run ``source`` inside the prelude's ``opened()``, with its names in scope."""
    module = load_prelude(prelude)
    # The block runs as a module of its own whose file is the page or
    # docstring it came from. A model class the block declares then finds its
    # source there: pydantic reads field docstrings from the class's source
    # (``use_attribute_docstrings``), and under ``__main__`` it would read
    # this runner instead.
    block = ModuleType("_ferro_docs_block")
    block.__file__ = path
    sys.modules[block.__name__] = block
    namespace = vars(block)
    namespace.update(
        (name, value)
        for name, value in vars(module).items()
        if not name.startswith("__")
    )
    # Pad the source so line numbers in a traceback are the page's own.
    # ``dont_inherit``: the block must not pick up this file's
    # ``from __future__ import annotations``, which would turn a declared
    # model's annotations into strings a reader's own module never has.
    code = compile(
        "\n" * (first_line - 1) + source,
        path,
        "exec",
        flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        dont_inherit=True,
    )
    async with module.opened():
        result = eval(code, namespace)  # noqa: S307 - the docs block is ours
        if inspect.iscoroutine(result):
            await result


def point_connect_at(url: str) -> None:
    """Send every ``ferro.connect`` call to ``url``, whatever URL it names."""
    import ferro

    connect = ferro.connect

    async def connect_to(_url: str, *args: object, **kwargs: object) -> None:
        await connect(url, *args, **kwargs)

    ferro.connect = connect_to


def main(argv: list[str]) -> None:
    prelude, path, first_line = argv
    url = os.environ.get("FERRO_DOCS_DATABASE_URL")
    if url:
        # Before the prelude imports connect, so its name is this one too.
        point_connect_at(url)
    asyncio.run(run_block(prelude, sys.stdin.read(), path, int(first_line)))


if __name__ == "__main__":
    main(sys.argv[1:])
