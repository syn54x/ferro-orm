"""Checks over docs examples: query lambdas, error checks and retired relations.

Four mistakes compile and run but teach the wrong thing:

- ``Post.where(lambda p: p.published)``: a query lambda with a one-letter
  parameter. The docs name it after the model, ``lambda post: post.published``.
- A ``try`` whose ``except`` asserts on the error but has no ``else`` that
  raises. When the error stops happening, the block still passes.
- A retired relation term: a code form of the old API such as ``BackRef(``,
  or a term the glossary lists under ``_Avoid_`` for *Foreign-key column*,
  *Forward relation*, *Reverse relation* or *Loaded relation*, such as
  "shadow column" or "back-reference".
- Awaiting a forward relation, ``await book.shelf``, or prose saying that
  awaiting one loads it. The relation is the instance; ``.load()`` fills it.

Dated records keep the old words and are exempt: the upgrading guide, the
changelog, ``docs/plans``, ``docs/brainstorms``, ``docs/research``,
``docs/rfc`` and the ADRs written before the relation vocabulary.

The checks read Python files (with the fences in their docstrings) and the
Python fences of Markdown pages; the retired-terms and awaited-relation
checks also read Markdown prose, docstring prose and, for terms, comments.
Run them from the command line:

    python -m tests._docs_checks docs/examples docs/pages src/ferro GLOSSARY.md docs/adr

Each violation prints as ``path:line: message``; any violation exits 1.
"""

from __future__ import annotations

import ast
import inspect
import io
import re
import sys
import tokenize
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


# The code forms of the retired relation API and what to write instead. They
# are matched in prose and found as calls in Python (`_retired_calls`).
_RETIRED_CODE_FORMS = {
    "ForeignKey(": 'Field(foreign_key=...) and Forward("<column>")',
    "BackRef(": 'Reverse("<column>")',
    "related_name=": "the column name in Forward, Reverse or ManyToMany(through=...)",
}
# The glossary entries whose `_Avoid_` terms the docs never use: each term is
# retired in favour of the entry's own name.
_GLOSSARY = Path(__file__).resolve().parents[1] / "GLOSSARY.md"
_RELATION_ENTRIES = (
    "Foreign-key column",
    "Forward relation",
    "Reverse relation",
    "Loaded relation",
)
_AVOID = "_Avoid_: "
# Avoided terms that are Python names: the retired API's, which a refusal
# documents by name (its call form is retired above), and another library's,
# which a comparison names. Each names an API, not the concept.
_PYTHON_NAMES = frozenset({"BackRef", "related_name", "select_related"})


@cache
def retired_terms() -> dict[str, str]:
    """Map each retired relation term, lowercased, to what to write instead.

    The code forms of the retired API, then every term the glossary lists
    under `_Avoid_` for the relation entries, except the Python names in
    `_PYTHON_NAMES`.
    """
    terms = {term.lower(): fix for term, fix in _RETIRED_CODE_FORMS.items()}
    lines = _GLOSSARY.read_text(encoding="utf-8").splitlines()
    for entry in _RELATION_ENTRIES:
        start = lines.index(f"**{entry}**:")
        avoid = next(line for line in lines[start:] if line.startswith(_AVOID))
        for term in avoid.removeprefix(_AVOID).split(", "):
            if term not in _PYTHON_NAMES:
                terms.setdefault(term.lower(), entry.lower())
    return terms


def _replacement(found: str) -> str:
    """What to write for the retired term ``found``, as the text spelled it."""
    terms = retired_terms()
    return terms.get(found.lower()) or terms[found.lower().replace("-", " ")]


@cache
def _retired_pattern() -> re.Pattern[str]:
    # Longest first, so `BackRef(` is one match and never also `BackRef`. A
    # space in a term also matches a hyphen ("foreign key relation" finds
    # "foreign-key relation"); a hyphen matches only itself, since "belongs
    # to" and "has many" are plain English where "Belongs-to" and "has-many"
    # are names.
    terms = sorted(retired_terms(), key=len, reverse=True)
    return re.compile(
        "|".join(re.escape(term).replace(r"\ ", "[- ]") for term in terms),
        re.IGNORECASE,
    )


# Dated records keep the old words: the upgrading guide, the changelog, the
# planning notes and every ADR merged before the relation vocabulary. ADRs
# from this number on were written with it.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DATED_RECORDS = frozenset({"upgrading.md", "changelog.md"})
_DATED_DIRECTORIES = frozenset({"plans", "brainstorms", "research", "rfc"})
_FIRST_CURRENT_ADR = 61


def _repo_parts(path: Path) -> tuple[str, ...]:
    """``path``'s parts from the repo root, such as ``("docs", "adr", ...)``.

    A path outside the repo is read as written, relative to where the lint
    runs.
    """
    try:
        return path.resolve().relative_to(_REPO_ROOT).parts
    except ValueError:
        return path.parts


def _dated_record(path: Path) -> bool:
    if path.name.lower() in _DATED_RECORDS:
        return True
    if _adr(path):
        return path.name[:4].isdigit() and int(path.name[:4]) < _FIRST_CURRENT_ADR
    parts = _repo_parts(path)
    return len(parts) > 2 and parts[0] == "docs" and parts[1] in _DATED_DIRECTORIES


def _adr(path: Path) -> bool:
    return _repo_parts(path)[:2] == ("docs", "adr")


def _docs_file(path: Path) -> bool:
    return _repo_parts(path)[:1] == ("docs",)


_INLINE_CODE = re.compile(r"`[^`]*`")


def _retired(path: Path, line: int, term: str) -> Violation:
    fix = _replacement(term)
    return Violation(path, line, f'"{term}" is retired; write "{fix}"')


def _retired_in_prose(path: Path, lines: dict[int, str]) -> list[Violation]:
    return [
        _retired(path, number, match.group())
        for number, text in lines.items()
        if not text.lstrip().startswith(_AVOID)
        for match in _retired_pattern().finditer(text)
    ]


def _imported_elsewhere(tree: ast.AST) -> set[str]:
    """The names a source imports from a module other than ``ferro``."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").split(".")[0] != "ferro"
        for alias in node.names
    }


def _called_name(call: ast.Call) -> str | None:
    """``ForeignKey`` for ``ForeignKey(...)`` or ``ferro.ForeignKey(...)``."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "ferro"
    ):
        return call.func.attr
    return None


def _retired_calls(path: Path, tree: ast.AST) -> list[Violation]:
    foreign = _imported_elsewhere(tree)
    violations = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        name = _called_name(call)
        if name in ("ForeignKey", "BackRef") and name not in foreign:
            violations.append(_retired(path, call.lineno, f"{name}("))
        violations += [
            _retired(path, keyword.value.lineno, "related_name=")
            for keyword in call.keywords
            if keyword.arg == "related_name"
        ]
    return violations


def _comments(source: str, first_line: int) -> dict[int, str]:
    """Each comment in ``source``, keyed by the file line it is on."""
    return {
        first_line + token.start[0] - 1: token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }


def _prose(path: Path, text: str) -> dict[int, str]:
    """The prose lines of ``path``, keyed by line number.

    Markdown outside its Python fences, and a Python docstring outside its
    Python fences. Comments are read separately (`_comments`).
    """
    lines = dict(enumerate(text.splitlines(), start=1))
    if path.suffix == ".md":
        prose = lines
        fences = fenced_examples(path, text)
    else:
        prose, fences = {}, []
        for node in ast.walk(_parse(text, path, 1)):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
                prose |= {number: lines[number] for number in span}
                docstring = "\n".join(lines[number] for number in span)
                fences += fenced_examples(path, docstring, line_offset=node.lineno - 1)
    for fence in fences:
        for number in range(fence.start_line, fence.end_line + 1):
            prose.pop(number, None)
    return prose


def retired_vocabulary(path: Path) -> list[Violation]:
    """Report each use of a retired relation term in ``path``.

    Prose (Markdown outside Python fences, a docstring outside its Python
    fences, and every comment in Python code) is matched for every term in
    `retired_terms`, except on a glossary `_Avoid_` line. Python is otherwise
    read as code: a call to ``ForeignKey`` or ``BackRef`` that the source did
    not import from another library, and any call with a ``related_name=``
    keyword. A string literal in code, such as a refusal message, is never
    matched. Dated records (`_dated_record`) are exempt, and a current ADR's
    code (its fences and inline code) quotes the form its decision replaced,
    so only its prose is matched.
    """
    if _dated_record(path):
        return []
    prose = _prose(path, path.read_text("utf-8"))
    if _adr(path):
        prose = {number: _INLINE_CODE.sub("", text) for number, text in prose.items()}
        return _retired_in_prose(path, prose)
    violations = _retired_in_prose(path, prose)
    for source, first_line in _sources(path):
        violations += _retired_calls(path, _parse(source, path, first_line))
        violations += _retired_in_prose(path, _comments(source, first_line))
    return sorted(violations, key=lambda violation: violation.line)


# An await word, then a load or a return, in one clause with no call between:
# "awaiting it loads the target", "Await `post.author` to load it",
# "Awaiting `post.author` returns the Author". Not "await `.all()` to load
# them", and not "can't be awaited, loaded or not".
_AWAITING_LOADS = re.compile(
    r"\bawait(?:s|ing|ed)?\b(?:"
    r"(?:(?!\w\(|[,.;:](?:\s|$)).)*?(?<![.\w])load(?:s|ed|ing)?\b"
    r"|\s+(?:`\w+(?:\.\w+)+`|it|the relation|a relation)\s+returns\b)",
    re.IGNORECASE,
)
_AWAITED_RELATION = (
    "awaiting a relation raises TypeError; write "
    '"await <instance>.<relation>.load()" or include() it in the query'
)


def _paragraphs(prose: dict[int, str]) -> Iterator[tuple[str, list[int]]]:
    """Join consecutive prose lines into paragraphs, a blank line ending one.

    Yields each paragraph's text and, per character, the file line it is on.
    """
    text, lines = "", []
    for number in sorted(prose):
        line = prose[number].strip()
        if not line or (lines and number != lines[-1] + 1):
            if text:
                yield text, lines
            text, lines = "", []
        if line:
            text += line + " "
            lines += [number] * (len(line) + 1)
    if text:
        yield text, lines


def awaited_relations(path: Path) -> list[Violation]:
    """Report each place ``path`` awaits a relation or says awaiting one loads it.

    In prose, a clause of a paragraph saying awaiting loads or returns; in
    code, an ``await`` of an attribute (``await book.shelf``) rather than of
    a call, read in docs files and in docstring fences, never in a module's
    own code. Dated records are exempt.
    """
    if _dated_record(path):
        return []
    violations = [
        Violation(path, lines[match.start()], _AWAITED_RELATION)
        for text, lines in _paragraphs(_prose(path, path.read_text("utf-8")))
        for match in _AWAITING_LOADS.finditer(text)
    ]
    sources = list(_sources(path))
    if path.suffix == ".py" and not _docs_file(path):
        sources = sources[1:]  # the module's own code
    for source, first_line in sources:
        violations += [
            Violation(path, node.lineno, _AWAITED_RELATION)
            for node in ast.walk(_parse(source, path, first_line))
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Attribute)
        ]
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
            violations += retired_vocabulary(path)
            violations += awaited_relations(path)
        except SyntaxError as error:
            violations.append(
                Violation(path, error.lineno or 0, f"does not parse: {error.msg}")
            )
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
