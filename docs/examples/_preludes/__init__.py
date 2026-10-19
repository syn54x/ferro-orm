"""Preludes for runnable docs blocks, and the runner that uses them.

A prelude is a module in this directory. It declares the models a docs block
uses and defines ``opened()``, an async context manager that connects to an
in-memory SQLite database, adds a few rows and opens a session. A block marked
``{test=run prelude=<name>}`` sees every name the prelude defines and runs
inside ``opened()``, with top-level ``await``.

Run a block by piping its source to this file:

    python docs/examples/_preludes/__init__.py <prelude> <path> <first line>

``path`` and ``first line`` are where the block's source sits, so a traceback
points at the docs page or docstring, not at this runner.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
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
    namespace = {
        name: value for name, value in vars(module).items() if not name.startswith("__")
    }
    namespace["__name__"] = "__main__"
    # Pad the source so line numbers in a traceback are the page's own.
    code = compile(
        "\n" * (first_line - 1) + source,
        path,
        "exec",
        flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
    )
    async with module.opened():
        result = eval(code, namespace)  # noqa: S307 - the docs block is ours
        if inspect.iscoroutine(result):
            await result


def main(argv: list[str]) -> None:
    prelude, path, first_line = argv
    asyncio.run(run_block(prelude, sys.stdin.read(), path, int(first_line)))


if __name__ == "__main__":
    main(sys.argv[1:])
