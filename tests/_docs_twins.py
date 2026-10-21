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
- a comment quoting a declaration (``# before: bio: str = Field(index=True)``)
  quotes it in the same idiom;
- everything else in the script (data, queries, prints, asserts) is the same
  text, so both twins print the same thing.

Run ``python -m tests._docs_twins docs/examples/<name>.py`` to write
``<name>_annotated.py`` after changing ``<name>.py``.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
import tokenize
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


def _module_name(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.ImportFrom):
        return node.module or ""
    return node.names[0].name


def _sorted_names(names: list[str]) -> list[str]:
    """``names`` in ruff's order: constants, then classes, then the rest."""
    return sorted(
        names,
        key=lambda name: (
            0 if name.isupper() and len(name) > 1 else 1 if name[0].isupper() else 2,
            name,
        ),
    )


def _import_groups(tree: ast.Module) -> list[tuple[list[ast.stmt], int]]:
    """Each run of top-level imports, with the line where the code it serves ends.

    An example whose regions each stand alone imports again at the top of each
    region, so each run serves the code up to the next run.
    """
    groups: list[list[ast.stmt]] = []
    previous_was_import = False
    for node in tree.body:
        is_import = isinstance(node, ast.Import | ast.ImportFrom)
        if is_import and not previous_was_import:
            groups.append([])
        if is_import:
            groups[-1].append(node)
        previous_was_import = is_import
    ends = [group[0].lineno - 1 for group in groups[1:]] + [10**9]
    return list(zip(groups, ends, strict=True))


def _shared_comment(lines: list[str], group: list[ast.stmt]) -> str:
    """The trailing comment every import of ``group`` carries (``# noqa: E402``
    on imports placed after code), to carry onto the one added beside them."""
    comments = {
        lines[(node.end_lineno or node.lineno) - 1].partition("  #")[2].rstrip("\n")
        for node in group
    }
    [comment] = comments if len(comments) == 1 else [""]
    return f"  #{comment}" if comment else ""


def _with_annotated_import(source: str) -> str:
    """``source`` importing ``Annotated`` from ``typing`` in every run of imports
    whose code uses it, where ruff's import order puts it."""
    tree = ast.parse(source)
    used = sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "Annotated"
    )
    lines = source.splitlines(keepends=True)
    edits: list[tuple[int, int, list[str]]] = []
    for group, ends_at in _import_groups(tree):
        starts_at = group[-1].end_lineno or group[-1].lineno
        if not any(starts_at < line <= ends_at for line in used):
            continue
        typing = next(
            (
                node
                for node in group
                if isinstance(node, ast.ImportFrom) and node.module == "typing"
            ),
            None,
        )
        if typing is not None:
            names = [alias.name for alias in typing.names]
            if "Annotated" not in names:
                names = _sorted_names([*names, "Annotated"])
                edits.append(
                    (
                        typing.lineno - 1,
                        typing.end_lineno or typing.lineno,
                        [
                            f"from typing import {', '.join(names)}"
                            f"{_shared_comment(lines, group)}\n"
                        ],
                    )
                )
            continue
        stdlib = [
            node
            for node in group
            if _module_name(node).split(".")[0] in sys.stdlib_module_names
            and _module_name(node) != "__future__"
        ]
        line = f"from typing import Annotated{_shared_comment(lines, group)}\n"
        if stdlib:
            after = [
                node
                for node in stdlib
                if isinstance(node, ast.ImportFrom) and _module_name(node) > "typing"
            ]
            at = after[0].lineno - 1 if after else stdlib[-1].end_lineno or 0
            edits.append((at, at, [line]))
        else:
            first = next(node for node in group if _module_name(node) != "__future__")
            edits.append((first.lineno - 1, first.lineno - 1, [line, "\n"]))
    for start, stop, text in sorted(edits, reverse=True):
        lines[start:stop] = text
    return "".join(lines)


_QUOTED = re.compile(r"^# (?P<lead>[a-z ]+: )(?P<code>\w+: .+)$")


def _quoted_declaration(comment: str) -> str | None:
    """A whole-line comment quoting a declaration (``# before: bio: str =
    Field(index=True)``), with the declaration in the Annotated idiom, or
    ``None`` when the comment quotes none."""
    match = _QUOTED.match(comment)
    if match is None:
        return None
    try:
        [node] = ast.parse(match["code"]).body
    except SyntaxError:
        return None
    if not isinstance(node, ast.AnnAssign) or node.value is None:
        return None
    declaration = _declaration(match["code"], node)
    if declaration is None:
        return None
    return f"# {match['lead']}{declaration}"


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
    comments = {
        token.start[0]: token
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }
    rewrites: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or node.value is None:
            continue
        declaration = _declaration(source, node)
        if declaration is None:
            continue
        end = node.end_lineno or node.lineno
        inside = [line for line in range(node.lineno, end) if line in comments]
        if inside:
            raise ValueError(
                f"line {inside[0]}: a comment inside a multi-line Field "
                "declaration would be lost; move it above the field"
            )
        trailing = f"  {comments[end].string}" if end in comments else ""
        indent = lines[node.lineno - 1][: node.col_offset]
        rewrites.append((node.lineno, end, f"{indent}{declaration}{trailing}\n"))
    for line, token in comments.items():
        quoted = _quoted_declaration(token.string)
        if quoted is not None and not any(s <= line <= e for s, e, _ in rewrites):
            start = token.start[1]
            text = lines[line - 1]
            rewrites.append((line, line, f"{text[:start]}{quoted}\n"))
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
