"""The Annotated twin of a docs example script, derived from its Assignment form.

AGENTS.md I-7 shows every field-declaring example in two tabs, and the two
differ only in where the field options sit. A twin is therefore not written by
hand: ``annotated_twin`` rewrites each ``name: T = Field(...)`` of the
Assignment script into the one Annotated idiom of
``docs/solutions/patterns/docs-page-conventions.md``:

- options move inside ``Annotated[T, Field(...)]``;
- a ``default`` stays the assigned value (``= None``), where type checkers
  see it;
- a ``default_factory`` stays an assigned ``Field(default_factory=...)``, for
  the same reason;
- everything else in the script (data, queries, prints, asserts) is the same
  text, so both twins print the same thing.

Run ``python -m tests._docs_twins docs/examples/<name>.py`` to write
``<name>_annotated.py`` after changing ``<name>.py``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_EXAMPLES = REPO_ROOT / "docs" / "examples"

_FIELD_CALLS = frozenset({"Field", "FerroField"})
_ASSIGNED = frozenset({"default", "default_factory"})


def twin_pairs() -> list[tuple[Path, Path]]:
    """Every ``docs/examples/<name>.py`` that has a ``<name>_annotated.py``."""
    return [
        (path.with_name(path.name.removesuffix("_annotated.py") + ".py"), path)
        for path in sorted(DOCS_EXAMPLES.glob("*_annotated.py"))
    ]


def _segment(source: str, node: ast.AST) -> str:
    text = ast.get_source_segment(source, node)
    assert text is not None
    return text


def _declaration(source: str, node: ast.AnnAssign) -> str | None:
    """The Annotated form of one ``name: T = Field(...)``, or ``None`` when the
    statement is not one."""
    call = node.value
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id in _FIELD_CALLS
    ):
        return None
    if any(keyword.arg is None for keyword in call.keywords):
        raise ValueError(
            f"line {node.lineno}: a **kwargs Field call has no Annotated twin; "
            "spell the options out"
        )
    default: str | None = None
    if call.args:
        if len(call.args) > 1:
            raise ValueError(f"line {node.lineno}: Field takes one positional default")
        default = _segment(source, call.args[0])
    factory: str | None = None
    options: list[str] = []
    for keyword in call.keywords:
        value = _segment(source, keyword.value)
        if keyword.arg == "default":
            default = value
        elif keyword.arg == "default_factory":
            factory = value
        else:
            options.append(f"{keyword.arg}={value}")
    target = _segment(source, node.target)
    annotation = _segment(source, node.annotation)
    if options:
        metadata = f"{call.func.id}({', '.join(options)})"
        if (
            isinstance(node.annotation, ast.Subscript)
            and isinstance(node.annotation.value, ast.Name)
            and node.annotation.value.id == "Annotated"
        ):
            annotation = f"{annotation[:-1]}, {metadata}]"
        else:
            annotation = f"Annotated[{annotation}, {metadata}]"
    declaration = f"{target}: {annotation}"
    if factory is not None:
        declaration += f" = {call.func.id}(default_factory={factory})"
    elif default is not None:
        declaration += f" = {default}"
    return declaration


def _with_annotated_import(source: str) -> str:
    """``source`` importing ``Annotated`` from ``typing``, where ruff's import
    order puts it."""
    tree = ast.parse(source)
    imports = [
        node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    for node in imports:
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            names = [alias.name for alias in node.names]
            if "Annotated" in names:
                return source
            names.append("Annotated")
            names.sort(
                key=lambda name: (
                    0
                    if name.isupper() and len(name) > 1
                    else 1
                    if name[0].isupper()
                    else 2,
                    name,
                )
            )
            lines = source.splitlines(keepends=True)
            lines[node.lineno - 1 : node.end_lineno] = [
                f"from typing import {', '.join(names)}\n"
            ]
            return "".join(lines)
    stdlib = [
        node
        for node in imports
        if (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name)
        and (
            (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name)
            or ""
        ).split(".")[0]
        in sys.stdlib_module_names
        and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
    ]
    lines = source.splitlines(keepends=True)
    if stdlib:
        after = [
            node
            for node in stdlib
            if isinstance(node, ast.ImportFrom) and (node.module or "") > "typing"
        ]
        if after:
            lines.insert(after[0].lineno - 1, "from typing import Annotated\n")
        else:
            lines.insert(stdlib[-1].end_lineno or 0, "from typing import Annotated\n")
        return "".join(lines)
    first = next(
        node
        for node in imports
        if not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
    )
    lines.insert(first.lineno - 1, "from typing import Annotated\n\n")
    return "".join(lines)


def _formatted(source: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--stdin-filename", "twin.py", "-"],
        input=source,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"ruff format refused the twin:\n{result.stderr}")
    return result.stdout


def annotated_twin(source: str) -> str:
    """Rewrite an Assignment-style example script into its Annotated twin.

    Args:
        source: The Assignment script, formatted by ruff.

    Returns:
        The twin, formatted by ruff: every ``Field(...)`` declaration in the
        Annotated idiom, ``Annotated`` imported, every other line unchanged.

    Raises:
        ValueError: A declaration has no Annotated twin (``Field(**options)``),
            or a comment sits inside a declaration the rewrite would drop.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    rewrites: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or node.value is None:
            continue
        declaration = _declaration(source, node)
        if declaration is None:
            continue
        end = node.end_lineno or node.lineno
        span = "".join(lines[node.lineno - 1 : end])
        indent = lines[node.lineno - 1][: node.col_offset]
        rest = span.replace(_segment(source, node), "", 1)
        trailing = ""
        if "#" in rest:
            before, _, comment = rest.partition("#")
            if end != node.lineno or before.strip():
                raise ValueError(
                    f"line {node.lineno}: a comment inside a multi-line Field "
                    "declaration would be lost; move it above the field"
                )
            trailing = f"  #{comment.rstrip(chr(10))}"
        rewrites.append((node.lineno, end, f"{indent}{declaration}{trailing}\n"))
    if not rewrites:
        return source
    for start, end, text in sorted(rewrites, reverse=True):
        lines[start - 1 : end] = [text]
    return _formatted(_with_annotated_import("".join(lines)))


def main(argv: list[str]) -> None:
    for name in argv:
        path = Path(name)
        twin = path.with_name(f"{path.stem}_annotated.py")
        twin.write_text(annotated_twin(path.read_text(encoding="utf-8")), "utf-8")
        print(f"wrote {twin}")


if __name__ == "__main__":
    main(sys.argv[1:])
