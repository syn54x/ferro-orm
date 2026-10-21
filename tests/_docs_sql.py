"""What a docs example sends and prints, region by region, and the blocks that show it.

A page shows two things an example script does: the SQL a snippet region
sends (a ```sql block opening with ``-- <example>.py:<snippet> (<dialect>)``)
and what it prints (a ```text block under ``<!-- prints: <example>.py:<snippet> -->``).
Both are regenerated, never typed: ``captured`` runs the script once per
dialect in a subprocess, records each statement and each printed line at the
example line that caused it, and groups them by snippet region; ``check_block``
and ``check_printed`` compare a page's block with that record byte for byte.

A capture runs ``python -m tests._docs_sql <example> <dialect> <out.json> <url|-> [args...]``:
the script runs as ``__main__`` with ``args`` as its arguments and
``ferro.connect`` pointed at ``url`` unless it is ``-``, and the record is
written to ``out.json``.

What a block shows:

- A query terminal's statement as its ``.sql()`` renders it, and any other
  statement as the statement log records it. Schema statements, the Postgres
  catalog probe, session settings and a ``transaction()`` block's own
  ``BEGIN`` / ``COMMIT`` are left out (``HIDDEN_OPERATIONS``). A region that
  sends nothing but prints statements (a ``create_tables()`` report) shows
  what it prints.
- Each statement laid out one clause per line (``layout``), ending in ``;``,
  with a blank line between two.
- A printed line belongs to the innermost line of the example inside a region
  on the call stack, so a helper defined outside the region that prints for it
  counts for the region.
"""

from __future__ import annotations

import io
import json
import logging
import re
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests._docs_harness import (
    DOCS_PAGES,
    REPO_ROOT,
    Fence,
    fence_settings,
    fenced_examples,
    point_connect_at,
    run_fence,
    run_python,
    snippet_regions,
)

DOCS_EXAMPLES = REPO_ROOT / "docs" / "examples"

MARKER = re.compile(
    r"^-- (?P<example>[A-Za-z0-9_]+\.py):(?P<snippet>[A-Za-z0-9_-]+) "
    r"\((?P<dialect>sqlite|postgres)\)$"
)
"""The first line of a ```sql block: ``-- <example>.py:<snippet> (<dialect>)``."""

PRINTS = re.compile(
    r"^(?P<example>[A-Za-z0-9_]+\.py):(?P<snippet>[A-Za-z0-9_-]+)"
    r"(?: \((?P<dialect>postgres)\))?$"
)
"""The value of a ```text block's ``<!-- prints: <example>.py:<snippet> -->``
note, with `` (postgres)`` after it for what the region prints on PostgreSQL."""

HIDDEN_OPERATIONS = frozenset({"migrate", "catalog", "settings", "transaction"})
"""Statement-log operations a block does not show: schema statements, the
Postgres catalog probe, session settings and a transaction block's own
statements."""

CLAUSES = (
    "FROM",
    "INNER JOIN",
    "LEFT JOIN",
    "WHERE",
    "GROUP BY",
    "HAVING",
    "ORDER BY",
    "LIMIT",
    "OFFSET",
    "SET",
    "VALUES",
    "ON CONFLICT",
    "RETURNING",
)
"""The keywords that start a new line of a laid-out statement, at the top
level of the statement only (never inside parentheses or quotes)."""

PLACEHOLDER = re.compile(r"\$\d+|\?")
"""A bind placeholder: SQLite's ``?`` or PostgreSQL's ``$1``."""


# -- the layout ------------------------------------------------------------------


def layout(sql: str) -> str:
    """Lay ``sql`` out one clause per line.

    A line break replaces the single space before each top-level clause
    keyword (``CLAUSES``). A ``CREATE TABLE`` statement also puts each column
    and constraint of its parenthesised body on its own line, indented two
    spaces. Removing each line's indentation and joining the lines with
    single spaces gives back ``sql``.
    """
    table = sql.startswith("CREATE TABLE")
    breaks: dict[int, str] = {}
    depth = 0
    quote: str | None = None
    for index, char in enumerate(sql):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char != " ":
            continue
        elif depth == 0:
            rest = sql[index + 1 :]
            if any(
                rest.startswith(keyword)
                and (len(rest) == len(keyword) or rest[len(keyword)] in " (")
                for keyword in CLAUSES
            ):
                breaks[index] = "\n"
        elif table and depth == 1:
            # At depth 1 the body's own "(" or a "," between its items comes
            # before the space; its closing ")" comes after it.
            if sql[index - 1] in "(,":
                breaks[index] = "\n  "
            elif sql[index + 1 : index + 2] == ")":
                breaks[index] = "\n"
    return "".join(breaks.get(index, char) for index, char in enumerate(sql))


def unlaid(laid: str) -> str:
    """Return the statement ``layout`` laid out: its lines unindented and joined."""
    return " ".join(line.lstrip(" ") for line in laid.splitlines())


def statements(sqls: list[str]) -> str:
    """Return the body a ```sql block holds for ``sqls``.

    Each statement is laid out and ends in ``;``, with a blank line between two.
    """
    return "\n".join(f"{layout(sql)};\n" for sql in sqls)


def region_lines(source: str, *, where: str = "<source>") -> dict[str, tuple[int, int]]:
    """Return each snippet region of an example's ``source`` as its marker lines."""
    return {
        name: (region.start, region.end)
        for name, region in snippet_regions(source, where=where).items()
    }


# -- what a run records ----------------------------------------------------------


@dataclass
class Region:
    """What one snippet region of an example did during a run."""

    sent: list[str] = field(default_factory=list)
    """The statements it sent, in order."""
    printed: list[str] = field(default_factory=list)
    """The lines it printed, in order, without their line ends."""

    @property
    def shown_sql(self) -> list[str]:
        """The statements a ```sql block shows: what it sent, or, for a region
        that sends nothing, the statements it prints."""
        return self.sent or self.printed

    @property
    def shown_output(self) -> str:
        """The text a ```text block under ``<!-- prints: ... -->`` shows."""
        return "".join(f"{line}\n" for line in self.printed)


@dataclass
class Run:
    """Everything one run of an example script did."""

    regions: dict[str, Region]
    """Each snippet region's statements and printed lines."""
    stdout: str
    """Everything the script printed."""
    schemas: list[Any]
    """The registered models' SchemaIR each time the script connected or
    created tables, and once at its end, consecutive repeats dropped."""


class Capture:
    """What one example run sends and prints, attributed to the example lines."""

    def __init__(self, path: Path, dialect: str) -> None:
        self.path = str(path)
        self.dialect = dialect
        self.root = str(path.parent)
        self.ranges = region_lines(path.read_text(encoding="utf-8"), where=self.path)
        self.where: tuple[str, int] | None = None
        """The docs example file and line running now."""
        self.depth = 0
        """How many terminals are running; their own statements are rendered."""
        self.sent: list[tuple[int, str]] = []
        self.printed: list[tuple[int, str]] = []
        self.stdout = ""
        self.pending = ""
        self.schemas: list[Any] = []

    def on_line(self, code: Any, line: int) -> Any:
        """Note the example line running now (a ``sys.monitoring`` LINE callback)."""
        if code.co_filename.startswith(self.root):
            self.where = (code.co_filename, line)
            return None
        return sys.monitoring.DISABLE

    def record(self, sql: str) -> None:
        if self.where is not None and self.where[0] == self.path:
            self.sent.append((self.where[1], sql))

    @property
    def line(self) -> str:
        """The example line running now, as ``<example>:<line>``."""
        if self.where is None:
            return f"{Path(self.path).name}:?"
        return f"{Path(self.where[0]).name}:{self.where[1]}"

    async def run_terminal(
        self, render: Callable[[], Awaitable[Any]], call: Awaitable[Any]
    ) -> Any:
        """Record what a terminal sends, rendered by its ``.sql()``, then run it.

        A terminal another terminal runs is not rendered again: the outer
        rendering already holds its statement. Its own statements reach the
        statement log while ``depth`` is up, and are not recorded twice.

        When ``.sql()`` raises, the call must raise the same exception type,
        having sent nothing; a call that then succeeds, or fails otherwise,
        would send a statement no block could show, so it fails the capture
        naming the example line.
        """
        if self.depth:
            return await call
        self.depth += 1
        try:
            line = self.line
            try:
                rendered = await render()
            except Exception as refused:
                try:
                    result = await call
                except type(refused):
                    raise
                except Exception as other:
                    raise AssertionError(
                        f"{line}: .sql() raised {refused!r} but the call raised "
                        f"{other!r}; a terminal must refuse the same way in both"
                    ) from other
                raise AssertionError(
                    f"{line}: .sql() raised {refused!r} but the call succeeded "
                    f"(returned {result!r}), so what it sent cannot be shown"
                ) from refused
            assert rendered.dialect == self.dialect, (
                f"{line}: rendered for {rendered.dialect}, ran on {self.dialect}"
            )
            self.record(str(rendered))
            return await call
        finally:
            self.depth -= 1

    def _printing_line(self) -> int | None:
        """The example line a print belongs to: the innermost one on the stack
        inside a region, else the line running now."""
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_filename == self.path and any(
                start < frame.f_lineno < end for start, end in self.ranges.values()
            ):
                return frame.f_lineno
            frame = frame.f_back
        if self.where is not None and self.where[0] == self.path:
            return self.where[1]
        return None

    def write(self, text: str) -> None:
        """Record each line the example prints, at the example line printing it."""
        self.stdout += text
        self.pending += text
        *lines, self.pending = self.pending.split("\n")
        if lines:
            line = self._printing_line()
            if line is not None:
                self.printed.extend((line, printed) for printed in lines)

    def on_statement(self, record: logging.LogRecord) -> None:
        operation = getattr(record, "operation", None)
        if self.depth == 0 and operation not in HIDDEN_OPERATIONS:
            assert record.dialect == self.dialect, record.dialect  # type: ignore[attr-defined]
            self.record(record.sql)  # type: ignore[attr-defined]

    def snapshot(self) -> None:
        """Record the registered models' SchemaIR, unless it is the last one."""
        from ferro.ir.compiler import compile_registry_schema_ir

        try:
            ir = compile_registry_schema_ir()
        except TypeError as refused:
            # A model the script declared and Ferro refused (two primary keys)
            # stays registered; the refusal is the schema the script ends with.
            ir = {"refused": str(refused)}
        schema = json.loads(json.dumps(ir, default=str))
        if not self.schemas or self.schemas[-1] != schema:
            self.schemas.append(schema)

    def by_region(self) -> dict[str, Region]:
        """Return what each snippet region sent and printed."""
        return {
            name: Region(
                sent=[sql for line, sql in self.sent if start < line < end],
                printed=[text for line, text in self.printed if start < line < end],
            )
            for name, (start, end) in self.ranges.items()
        }

    def run(self) -> Run:
        """Return the record of the run so far."""
        return Run(self.by_region(), self.stdout, self.schemas)


def _run_capture(path: Path, dialect: str, url: str | None, args: list[str]) -> Run:
    """Run the example at ``path`` here, as ``__main__``, and return its record."""
    import runpy

    import ferro
    from ferro.query.rendered import Terminal

    capture = Capture(path, dialect)

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            capture.on_statement(record)

    logger = logging.getLogger("ferro.sql")
    logger.addHandler(_Handler())
    logger.setLevel(logging.DEBUG)

    call = Terminal.__call__

    def render_then_call(self: Terminal, *args: Any, **kwargs: Any) -> Any:
        # Called first, so a terminal that refuses its query before returning
        # an awaitable raises here, where the example expects it.
        awaitable = call(self, *args, **kwargs)

        return capture.run_terminal(lambda: self.sql(*args, **kwargs), awaitable)

    Terminal.__call__ = render_then_call  # type: ignore[method-assign]

    create_tables = ferro.create_tables

    async def create_then_snapshot(*args: Any, **kwargs: Any) -> Any:
        capture.snapshot()
        return await create_tables(*args, **kwargs)

    point_connect_at(url, before=capture.snapshot)
    ferro.create_tables = create_then_snapshot  # type: ignore[assignment]

    tool = 4
    sys.monitoring.use_tool_id(tool, "docs-capture")
    sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, capture.on_line)
    sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
    sys.path.insert(0, str(path.parent))
    sys.argv = [str(path), *args]
    stdout = sys.stdout

    class _Tee(io.TextIOBase):
        def write(self, text: str) -> int:
            capture.write(text)
            return stdout.write(text)

    sys.stdout = _Tee()
    try:
        runpy.run_path(str(path), run_name="__main__")
    finally:
        sys.stdout = stdout
        sys.monitoring.set_events(tool, 0)
        sys.monitoring.free_tool_id(tool)
    capture.snapshot()
    return capture.run()


_captured: dict[tuple[str, str, str | None, tuple[str, ...]], Run] = {}
"""Each example's run per dialect and database, made once per test process."""


def captured(
    example: str | Path,
    dialect: str = "sqlite",
    url: str | None = None,
    args: tuple[str, ...] = (),
) -> Run:
    """Run an example script once in a subprocess and return what it did.

    Args:
        example: A script name in ``docs/examples`` (``predicates.py``), or a
            path to a script elsewhere.
        dialect: The dialect the script runs on, ``sqlite`` or ``postgres``.
        url: The database ``ferro.connect`` is pointed at. Defaults to the URL
            the script passes.
        args: The script's command-line arguments (``sys.argv[1:]``).

    Returns:
        The run's record, cached for the rest of the test process.

    Raises:
        AssertionError: The script failed; the message carries its output.
    """
    path = example if isinstance(example, Path) else DOCS_EXAMPLES / example
    key = (str(path), dialect, url, args)
    if key not in _captured:
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / "run.json"
            command = ["-m", "tests._docs_sql", str(path), dialect, str(out)]
            command += [url or "-", *args]
            run_python(command)
            record = json.loads(out.read_text(encoding="utf-8"))
        _captured[key] = Run(
            regions={
                name: Region(**region) for name, region in record["regions"].items()
            },
            stdout=record["stdout"],
            schemas=record["schemas"],
        )
    return _captured[key]


# -- the pages' blocks ---------------------------------------------------------------


def page_name(fence: Fence) -> str:
    """The block's page relative to ``docs/pages``, as ``<page>:<line>``."""
    try:
        page = Path(fence.path).relative_to(DOCS_PAGES).as_posix()
    except ValueError:
        page = str(fence.path)
    return f"{page}:{fence.line}"


def sql_marker(block: Fence) -> re.Match[str] | None:
    """The ``-- <example>.py:<snippet> (<dialect>)`` first line of a ```sql block."""
    return MARKER.match(block.body.partition("\n")[0])


def _named_region(where: str, example: str, snippet: str, named: str) -> None:
    path = DOCS_EXAMPLES / example
    assert path.is_file(), f"{named}: docs/examples/{example} does not exist"
    ranges = snippet_regions(path.read_text(encoding="utf-8"), where=str(path))
    assert snippet in ranges, f"{named}: {example} has no snippet region {snippet!r}"


def check_block(block: Fence, run: Callable[[str, str], Run] = captured) -> None:
    """Assert a ```sql block is marked and holds what its example's region sends.

    Args:
        block: The page's block.
        run: Given an example script's name and a dialect, its run's record.
            Defaults to ``captured`` on the dialect's own database.

    Raises:
        AssertionError: The block has no marker, names an example or region
            that does not exist, or differs from what the region sends. The
            message names the page, the line and the example.
    """
    where = page_name(block)
    marker = sql_marker(block)
    assert marker is not None, (
        f"{where}: a ```sql block must open with a marker line "
        f"'-- <example>.py:<snippet> (<dialect>)' naming where its SQL comes "
        f"from; this one opens with {block.body.partition(chr(10))[0]!r}"
    )
    example, snippet, dialect = marker["example"], marker["snippet"], marker["dialect"]
    named = f"{where} ({example}:{snippet}, {dialect})"
    _named_region(where, example, snippet, named)
    region = run(example, dialect).regions.get(snippet, Region())
    sent = region.shown_sql
    assert sent, f"{named}: the region {snippet!r} of {example} sends nothing"
    expected = statements(sent)
    shown = block.body.partition("\n")[2]
    assert shown == expected, (
        f"{named}: the block differs from what {example}:{snippet} sends on "
        f"{dialect}. It sends:\n{expected}"
    )


def check_same_on_postgres(block: Fence, run: Callable[[str, str], Run]) -> None:
    """Assert a SQLite ```sql block stands for PostgreSQL too.

    The region must send the same statements there once each placeholder is
    read as ``?``. A region whose SQL differs in more than its placeholders
    shows a ``(postgres)`` block of its own, and this check does not apply to it.

    Raises:
        AssertionError: PostgreSQL's statements differ in more than their
            placeholders. The message names the page, the line and the example.
    """
    where = page_name(block)
    marker = sql_marker(block)
    assert marker is not None, f"{where}: a ```sql block has no marker line"
    example, snippet = marker["example"], marker["snippet"]
    sent = run(example, "postgres").regions.get(snippet, Region()).shown_sql
    expected = PLACEHOLDER.sub("?", statements(sent))
    shown = block.body.partition("\n")[2]
    assert PLACEHOLDER.sub("?", shown) == expected, (
        f"{where} ({example}:{snippet}, sqlite): the block stands for both "
        f"databases, but on postgres the region sends more than other "
        f"placeholders; show a (postgres) block beside it. It sends:\n"
        f"{statements(sent)}"
    )


NOT_PRINTED = "shows"
"""The note kind of a ```text block holding text Ferro does not print (a
sketch, a file tree, another tool's shell session, a prompt to paste), which
says what it shows instead: ``<!-- shows: the directory tree -->``. Whatever
Ferro prints, raises or warns is a ``prints:`` block."""

TEXT_NOTES = {
    "prints": (
        "what an example region, or the run fence above, prints; regenerated by "
        "tests/test_docs_output.py"
    ),
    "output": "what ferro migrate prints, regenerated by tests/test_docs_migrations.py",
    "refusal": "a refusal ferro prints, regenerated by tests/test_docs_migrations.py",
    NOT_PRINTED: "text Ferro does not print; the note says what it shows",
}
"""Every note a ```text block can carry, and what checks it."""


def check_printed(
    block: Fence,
    run: Callable[[str, str], Run] = captured,
    *,
    dialect: str = "sqlite",
    database_url: str | None = None,
) -> None:
    """Assert a ```text block says where it comes from, and holds what it says.

    A block under ``<!-- prints: <example>.py:<snippet> -->`` must equal what
    that region prints on SQLite, byte for byte, and one under
    ``<!-- prints: <example>.py:<snippet> (postgres) -->`` what it prints on
    PostgreSQL. One under ``<!-- prints: above -->`` must equal what the
    nearest Python fence above it prints, a fence marked
    ``{test=run prelude=<name>}``. A block under another note
    (``TEXT_NOTES``) is checked elsewhere, or says what it shows instead.

    Args:
        block: The page's ```text block.
        run: Given an example script's name and a dialect, its run's record.
            Defaults to ``captured``.
        dialect: The database this check runs against. A ``prints:`` block for
            the other one only has its example and region checked, or its
            fence found.
        database_url: The PostgreSQL database a ``prints: above`` block's
            fence runs against when that fence is marked ``backend=postgres``.

    Raises:
        AssertionError: The block has no note, an unknown one, names an
            example, region or fence that does not exist, or differs from what
            it prints. The message names the page and the line.
    """
    where = page_name(block)
    notes = ", ".join(f"<!-- {kind}: ... -->" for kind in TEXT_NOTES)
    assert block.note is not None, (
        f"{where}: a ```text block must sit under a note saying where it comes "
        f"from: <!-- prints: <example>.py:<snippet> --> for what an example "
        f"region prints, <!-- prints: above --> for what the run fence above "
        f"prints, or <!-- shows: <what it is> --> for text no script prints "
        f"(one of {notes})"
    )
    kind, value = block.note
    assert kind in TEXT_NOTES, f"{where}: unknown note {kind!r}; use one of {notes}"
    if kind != "prints":
        return
    if value == "above":
        _check_printed_above(block, where, dialect, database_url)
        return
    marker = PRINTS.match(value)
    assert marker is not None, (
        f"{where}: <!-- prints: {value} --> must name <example>.py:<snippet>, "
        "or say above"
    )
    example, snippet = marker["example"], marker["snippet"]
    named = f"{where} ({example}:{snippet})"
    _named_region(where, example, snippet, named)
    if (marker["dialect"] or "sqlite") != dialect:
        return
    printed = run(example, dialect).regions.get(snippet, Region()).shown_output
    assert printed, f"{named}: the region {snippet!r} of {example} prints nothing"
    assert block.body == printed, (
        f"{named}: the block differs from what {example}:{snippet} prints. "
        f"It prints:\n{printed}"
    )


def printing_fence(block: Fence) -> Any:
    """The Python fence a ``<!-- prints: above -->`` block shows the output of:
    the nearest one above it, or ``None``."""
    path = Path(block.path)
    fences = [
        example
        for example in fenced_examples(path, path.read_text(encoding="utf-8"))
        if example.start_line < block.line
    ]
    return fences[-1] if fences else None


def on_postgres(fence: Any) -> bool:
    """Whether a Python fence is marked ``backend=postgres``."""
    return fence_settings(fence).get("backend") == "postgres"


def _check_printed_above(
    block: Fence, where: str, dialect: str, database_url: str | None
) -> None:
    above = printing_fence(block)
    assert above is not None, (
        f"{where}: <!-- prints: above --> has no Python fence above it"
    )
    assert fence_settings(above).get("test") == "run", (
        f"{where}: <!-- prints: above --> needs the fence above "
        f"(line {above.start_line}) marked {{test=run prelude=<name>}}"
    )
    if dialect != ("postgres" if on_postgres(above) else "sqlite"):
        return
    printed = run_fence(above, database_url=database_url)
    assert block.body == printed, (
        f"{where}: the block differs from what the fence at line "
        f"{above.start_line} prints. It prints:\n{printed}"
    )


if __name__ == "__main__":
    script, dialect, out, url, *args = sys.argv[1:]
    result = _run_capture(
        Path(script).resolve(), dialect, None if url == "-" else url, args
    )
    Path(out).write_text(
        json.dumps(
            {
                "regions": {
                    name: {"sent": region.sent, "printed": region.printed}
                    for name, region in result.regions.items()
                },
                "stdout": result.stdout,
                "schemas": result.schemas,
            }
        ),
        encoding="utf-8",
    )
