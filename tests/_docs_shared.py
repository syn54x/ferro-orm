"""The two pieces the docs harness shares with the scripts it runs.

- ``snippet_regions`` reads the snippet-region grammar
  (``--8<-- [start:<name>]`` … ``--8<-- [end:<name>]``) of an example script
  or a Markdown file. The harness (``_docs_harness``, ``_docs_sql``), the
  docs tests and ``docs/examples/_migrations_project.py`` all read regions
  through it.
- ``point_connect_at`` points every ``ferro.connect`` call at the test
  database. The SQL and output capture (``python -m tests._docs_sql``) and
  the prelude runner (``docs/examples/_preludes/__init__.py``) both install it.

It imports nothing beyond the standard library at import time, so a script
the harness starts (an example helper, the prelude runner) imports it without
loading the harness's own dependencies. ``tests._docs_harness`` re-exports both.
"""

from __future__ import annotations

import atexit
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# -- snippet regions ------------------------------------------------------------------

SNIPPET = re.compile(r"--8<--\s*\[(?P<edge>start|end):(?P<name>[A-Za-z0-9_-]+)\]")
"""A snippet region's start or end marker: ``# --8<-- [start:<name>]`` in a
script, ``<!-- --8<-- [start:<name>] -->`` in Markdown."""


@dataclass(frozen=True)
class SnippetRegion:
    """One snippet region of a file."""

    start: int
    """The line of its ``[start:<name>]`` marker, counting from 1."""
    end: int
    """The line of its ``[end:<name>]`` marker."""
    body: str
    """The lines between the two markers, as written, each with its line end."""


def snippet_regions(text: str, *, where: str = "<text>") -> dict[str, SnippetRegion]:
    """Read every snippet region of ``text``, by name.

    A region runs from the line holding ``--8<-- [start:<name>]`` to the line
    holding ``--8<-- [end:<name>]``; regions may nest or overlap. One marker per
    line is read.

    Args:
        text: An example script or a Markdown file.
        where: The file's name, for the error message.

    Returns:
        Each region's marker lines and body, in the order the regions start.

    Raises:
        ValueError: A name starts twice, ends without starting, or never
            ends. The message names the file, the line and the region.
    """
    lines = text.splitlines(keepends=True)
    open_at: dict[str, int] = {}
    regions: dict[str, SnippetRegion] = {}
    order: list[str] = []
    for number, line in enumerate(lines, start=1):
        marker = SNIPPET.search(line)
        if marker is None:
            continue
        name = marker["name"]
        if marker["edge"] == "start":
            if name in open_at or name in regions:
                raise ValueError(
                    f"{where}:{number}: the snippet region {name!r} starts a "
                    "second time; give each region its own name"
                )
            open_at[name] = number
            order.append(name)
            continue
        if name not in open_at:
            raise ValueError(
                f"{where}:{number}: the snippet region {name!r} ends without "
                f"starting; add '--8<-- [start:{name}]' above it"
            )
        start = open_at.pop(name)
        regions[name] = SnippetRegion(start, number, "".join(lines[start : number - 1]))
    if open_at:
        name, number = next(iter(open_at.items()))
        raise ValueError(
            f"{where}:{number}: the snippet region {name!r} never ends; add "
            f"'--8<-- [end:{name}]' below it"
        )
    return {name: regions[name] for name in order}


# -- the connect redirect -------------------------------------------------------------


def redirected_url(named: str, target: str) -> str:
    """Return the URL a ``connect(named)`` reaches when pointed at ``target``.

    The database is ``target``'s. When both name PostgreSQL, the query options
    ``named`` carries are set on it too, over ``target``'s own, so a block
    that connects with ``?ferro_search_path=tenant_a`` runs with that option.
    Options of another database's URL (SQLite's ``mode=rwc``) mean nothing to
    ``target`` and are left behind.
    """
    if not (_is_postgres(named) and _is_postgres(target)):
        return target
    shown = dict(parse_qsl(urlsplit(named).query, keep_blank_values=True))
    if not shown:
        return target
    parts = urlsplit(target)
    options = dict(parse_qsl(parts.query, keep_blank_values=True)) | shown
    return urlunsplit(parts._replace(query=urlencode(options)))


def _is_postgres(url: str) -> bool:
    return urlsplit(url).scheme in ("postgres", "postgresql")


def point_connect_at(
    url: str | None, *, before: Callable[[], object] | None = None
) -> None:
    """Send every ``ferro.connect`` call to ``url``, whatever URL it names.

    The URL is ``redirected_url(<the one named>, url)``. A
    ``ferro_search_path`` it carries names a schema the test database may
    lack, so the schema is created before the connect and dropped when the
    process exits. Install it before anything imports ``connect`` by name.

    Args:
        url: The database to connect to. ``None`` keeps the URL each call
            names, so only ``before`` is added.
        before: Called with no arguments before each connect.
    """
    import ferro

    connect = ferro.connect

    async def connect_to(named: str, *args: Any, **kwargs: Any) -> Any:
        if before is not None:
            before()
        reached = named if url is None else redirected_url(named, url)
        if url is not None and reached != url:
            _create_search_path_schema(reached)
        return await connect(reached, *args, **kwargs)

    ferro.connect = connect_to  # type: ignore[assignment]


_created_schemas: set[tuple[str, str]] = set()


def _create_search_path_schema(url: str) -> None:
    parts = urlsplit(url)
    options = dict(parse_qsl(parts.query, keep_blank_values=True))
    schema = options.pop("ferro_search_path", None)
    if schema is None or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        return  # connect() itself refuses a value that is not one identifier
    server = urlunsplit(parts._replace(query=urlencode(options)))
    if (server, schema) in _created_schemas:
        return
    import psycopg

    with psycopg.connect(server, autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)
        ).fetchone()
        if exists:
            return  # the test's own schema, or one this run does not own
        admin.execute(f'CREATE SCHEMA "{schema}"')
    _created_schemas.add((server, schema))
    atexit.register(_drop_schema, server, schema)


def _drop_schema(server: str, schema: str) -> None:
    import psycopg

    with psycopg.connect(server, autocommit=True) as admin:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
