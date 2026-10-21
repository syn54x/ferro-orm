"""Checks over docs examples: query lambdas and error checks.

Two mistakes compile and run but teach the wrong thing:

- ``Post.where(lambda p: p.published)``: a query lambda with a one-letter
  parameter. The docs name it after the model, ``lambda post: post.published``.
- A ``try`` whose ``except`` asserts on the error but has no ``else`` that
  raises. When the error stops happening, the block still passes.

Both checks read Python files (with the fences in their docstrings) and the
Python fences of Markdown pages. Run them from the command line:

    python -m tests._docs_checks docs/examples docs/pages src/ferro

Each violation prints as ``path:line: message``; any violation exits 1.
"""

from __future__ import annotations

import ast
import inspect
import sys
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from tests._docs_harness import fenced_examples

# Query methods the docs call with a lambda whose signature does not say so
# (aggregate projections, keyset paging). The signatures add the rest.
_NAMED_QUERY_METHODS = frozenset(
    {
        "where",
        "select",
        "order_by",
        "include",
        "exists",
        "count",
        "aggregate",
        "group_by",
        "after",
        "before",
    }
)
_CALLABLE_ANNOTATIONS = ("Callable", "Predicate", "RowSelector")


@dataclass(frozen=True)
class Violation:
    """One finding: where it is and what to change."""

    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


@cache
def query_methods_taking_callables() -> frozenset[str]:
    """Name every public ``Query`` or ``Model`` method that takes a callable.

    Read from the signatures (overloads included), plus the names the docs
    use on aggregate projections and keyset paging.
    """
    from ferro.models import Model, ModelConnection
    from ferro.query import ProjectedQuery, Query, Relation

    names = set(_NAMED_QUERY_METHODS)
    for cls in (Query, ProjectedQuery, Relation, Model, ModelConnection):
        for name, member in vars(cls).items():
            if name.startswith("_"):
                continue
            function = getattr(member, "__func__", member)
            if not callable(function):
                continue
            for signature_of in (function, *typing.get_overloads(function)):
                try:
                    signature = inspect.signature(signature_of)
                except (TypeError, ValueError):
                    continue
                if any(
                    marker in str(parameter.annotation)
                    for parameter in signature.parameters.values()
                    for marker in _CALLABLE_ANNOTATIONS
                ):
                    names.add(name)
    return frozenset(names)


def _sources(path: Path) -> Iterator[tuple[str, int]]:
    """Yield each Python source in ``path`` with the file line it starts on."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".md":
        for example in fenced_examples(path, text):
            # Snippet directives are expanded by the docs build; the file
            # they include is checked on its own.
            if "--8<--" not in example.source:
                yield example.source, example.start_line + 1
        return
    yield text, 1
    lines = text.splitlines(keepends=True)
    for node in ast.walk(_parse(text, path, 1)):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            docstring = "".join(lines[node.lineno - 1 : node.end_lineno])
            for example in fenced_examples(
                path, docstring, line_offset=node.lineno - 1
            ):
                yield example.source, example.start_line + 1


def _parse(source: str, path: Path, first_line: int) -> ast.AST:
    # Padding keeps every node's line number the file's own.
    return compile(
        "\n" * (first_line - 1) + source,
        str(path),
        "exec",
        flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
    )


def _trees(path: Path) -> Iterator[ast.AST]:
    for source, first_line in _sources(path):
        yield _parse(source, path, first_line)


def _supplied_parameters(function: ast.Lambda) -> list[ast.arg]:
    """The parameters the query fills: positional ones without a default.

    ``lambda post, f=field: getattr(post, f)`` captures ``f`` by default; the
    query only ever passes ``post``.
    """
    positional = [*function.args.posonlyargs, *function.args.args]
    return positional[: len(positional) - len(function.args.defaults)]


def lambda_param_violations(path: Path) -> list[Violation]:
    """Report each query lambda in ``path`` with a one-letter parameter."""
    methods = query_methods_taking_callables()
    violations = []
    for tree in _trees(path):
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in methods
            ):
                continue
            arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
            for argument in arguments:
                if not isinstance(argument, ast.Lambda):
                    continue
                for param in _supplied_parameters(argument):
                    if len(param.arg) < 2:
                        violations.append(
                            Violation(
                                path,
                                argument.lineno,
                                f"lambda passed to {call.func.attr}() takes "
                                f"one-letter parameter {param.arg!r}; name it "
                                "after the model in lowercase singular",
                            )
                        )
    return sorted(violations, key=lambda violation: violation.line)


def _asserts(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(node, ast.Assert) for stmt in handler.body for node in ast.walk(stmt)
    )


def _expected(handler: ast.ExceptHandler) -> str:
    if handler.type is None:
        return "an error"
    return ast.unparse(handler.type)


def unfailing_error_checks(path: Path) -> list[Violation]:
    """Report each ``try`` whose ``except`` asserts but whose ``else`` never raises."""
    violations = []
    for tree in _trees(path):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try | ast.TryStar):
                continue
            checking = [handler for handler in node.handlers if _asserts(handler)]
            if not checking:
                continue
            if any(isinstance(stmt, ast.Raise) for stmt in node.orelse):
                continue
            violations.append(
                Violation(
                    path,
                    node.lineno,
                    "try/except asserts on the error but has no else that raises, "
                    "so it passes when the error stops happening; add "
                    f'else: raise AssertionError("expected {_expected(checking[0])}")',
                )
            )
    return sorted(violations, key=lambda violation: violation.line)


def _files(paths: list[str]) -> Iterator[Path]:
    for name in paths:
        path = Path(name)
        if path.is_dir():
            yield from sorted(
                file
                for file in path.rglob("*")
                if file.suffix in {".py", ".md"} and file.is_file()
            )
        elif path.is_file():
            yield path
        else:
            raise SystemExit(f"not a file or directory: {name}")


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m tests._docs_checks PATH...", file=sys.stderr)
        return 2
    violations: list[Violation] = []
    for path in _files(argv):
        try:
            violations += lambda_param_violations(path)
            violations += unfailing_error_checks(path)
        except SyntaxError as error:
            violations.append(
                Violation(path, error.lineno or 0, f"does not parse: {error.msg}")
            )
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
