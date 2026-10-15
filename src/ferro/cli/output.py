"""How every ``ferro migrate`` verb prints: one presentation module, two forms.

```text
$ ferro migrate new add_team --data-step Author
main · migrations/main
✔ Wrote migrations/0002_add_team/
  01_expand.up.postgres.sql      schema step, up (postgres)
  02_backfill_author.py          backfill
  ir.json                        snapshot

  1 new model
    Team
```

Rich's console decides the form, from the stream and the environment; there
is no flag. The **plain form** (stdout is not a terminal: a pipe, a file, a
CI log) prints ASCII glyphs, no colour, and every name on its own line, and
keeps the one-liners (``ok: models match 0001_initial``), every refusal and
every ``warning: …`` line on stderr byte for byte. The **TTY form** (a
terminal, or ``FORCE_COLOR`` set) is headed: a glyph and a bold heading, the
details indented beneath, name lists in columns sized to the terminal, a long
change list collapsed behind ``--verbose``. ``NO_COLOR`` strips every escape
and keeps the layout; a non-UTF-8 or dumb terminal gets the ASCII glyphs;
``COLUMNS`` sets the width.

Words carry every meaning in both forms (a file's role, a list's count
heading, a problem's kind); colour only reinforces them. The tables below
are the one glyph table and the one colour table every verb reads.

The module reads report objects (:class:`GeneratedMigration`,
:class:`CheckReport`) and prints; it never changes them and never takes a
report's text apart.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, TextIO

from rich.console import Console
from rich.text import Text

if TYPE_CHECKING:
    from ..migrations.generate import CheckReport
    from ..migrations.layout import GeneratedMigration, GeneratedStep

__all__ = [
    "CHANGE_STYLE",
    "GLYPHS",
    "KIND_STYLE",
    "STATE_STYLE",
    "Output",
    "print_check",
    "print_new",
]

GLYPHS: dict[str, tuple[str, str]] = {
    "ok": ("✔", "ok"),
    "warning": ("▲", "!!"),
    "error": ("✘", "xx"),
    "pending": ("○", "--"),
    "partial": ("◐", "~~"),
    "bullet": ("•", "-"),
    "arrow": ("→", "->"),
}
"""Each glyph as ``(TTY, ASCII)``: the ASCII one in the plain form and on a
terminal that cannot show the other."""

GLYPH_STYLE = {
    "ok": "bold green",
    "warning": "bold yellow",
    "error": "bold red",
    "pending": "bold yellow",
    "partial": "bold yellow",
    "bullet": "dim",
    "arrow": "dim",
}

KIND_STYLE = {
    "ddl": "cyan",
    "data": "magenta",
    "portable_sql": "blue",
    "snapshot": "dim",
}
"""A step file's stem, by the kind its generated step records (``ddl``, a
schema step; ``data``, a Python step; ``portable_sql``, a hand-written SQL
step) or ``snapshot`` for ``ir.json``. A down file keeps its kind's hue,
dimmed; the suffix is always dim."""

CHANGE_STYLE = {
    "new": "green",
    "changed": "yellow",
    "dropped": "red",
    "removed": "red",
    "renamed": "blue",
}
"""A change summary's names, by the generator's verb: the colours ``git
status`` taught. The count heading above them stays plain bold."""

STATE_STYLE = {
    "applied": "green",
    "applied (baseline)": "green",
    "reverted": "green",
    "pending": "yellow",
    "failed": "bold red",
    "interrupted": "red",
    "applied (different checksum)": "red",
    "applied, not in the directory": "red",
    "running": "cyan",
    "reverting": "cyan",
    "nothing to reverse": "dim",
}
"""A migration's or a step's state word, as ``status``, ``up`` and ``down``
print it."""

LONG_LIST = 12
"""A change list of up to this many names is shown whole on a TTY."""
SHOWN_FIRST = 6
"""A longer one shows its first names, then how many more ``--verbose``
shows."""


class Output:
    """The two streams of one command, as Rich sees them.

    Build it once per command, inside the verb, with :meth:`for_streams`, so
    it wraps the streams the command really writes to.
    """

    def __init__(self, out: Console, err: Console) -> None:
        self.out = out
        self.err = err
        self.plain: bool = not out.is_terminal
        """The plain form: stdout is not a terminal."""
        self.width: int = out.width
        self._ascii = self.plain or out.options.ascii_only or out.is_dumb_terminal

    @classmethod
    def for_streams(
        cls, out: TextIO | None = None, err: TextIO | None = None
    ) -> Output:
        """The output of a command writing to ``out`` and ``err``
        (``sys.stdout`` and ``sys.stderr`` when omitted, read at the call)."""
        return cls(
            _console(sys.stdout if out is None else out),
            _console(sys.stderr if err is None else err),
        )

    # -- pieces ------------------------------------------------------------------

    def glyph(self, key: str) -> str:
        """The glyph ``key`` names in :data:`GLYPHS`, in this output's form."""
        tty, ascii_ = GLYPHS[key]
        return ascii_ if self._ascii else tty

    def file_name(self, name: str, *, kind: str) -> Text:
        """A step file's name: its stem coloured by ``kind`` (a key of
        :data:`KIND_STYLE`), its suffix dim, a down file its kind's hue
        dimmed."""
        stem, dot, suffix = name.partition(".")
        style = KIND_STYLE[kind]
        if suffix.startswith("down.") and kind != "snapshot":
            style += " dim"
        text = Text()
        text.append(stem, style=style)
        if dot:
            text.append(dot + suffix, style="dim")
        return text

    # -- lines -------------------------------------------------------------------

    def print(self, text: str | Text = "", *, err: bool = False) -> None:
        """Print one line. No form wraps it: a script reads what was said,
        and a command quoted in a note or a refusal is copied whole; a
        terminal folds a long line on its own."""
        (self.err if err else self.out).print(text)

    def heading(
        self, key: str, title: str | Text, where: str = "", *, err: bool = False
    ) -> None:
        """A result's heading: its glyph, a bold title (red for an error)
        and a dim ``where`` clause (``✔ Wrote migrations/0002_add_team/``)."""
        text = Text()
        text.append(self.glyph(key), style=GLYPH_STYLE[key])
        text.append(" ")
        if isinstance(title, Text):
            text.append_text(title)
        else:
            text.append(title, style="bold red" if key == "error" else "bold")
        if where:
            text.append(f"  {where}", style="dim")
        self.print(text, err=err)

    def database_line(self, name: str, where: str) -> None:
        """The first line of every verb: the database's name, bold, then
        what it touches, dim (``main · migrations/main``)."""
        from .migrate import database_line

        text = Text(database_line(name, where))
        text.stylize("bold", 0, len(name))
        text.stylize("dim", len(text) - len(where))
        self.print(text)

    def names(
        self,
        label_count: str,
        names: Sequence[str],
        *,
        verb: str,
        verbose: bool,
    ) -> None:
        """One change list: its count heading (``2 new models``), plain
        bold, then its names coloured by ``verb``. One name per line in the
        plain form; columns on a TTY, where a list longer than
        :data:`LONG_LIST` shows its first :data:`SHOWN_FIRST` unless
        ``verbose``."""
        self.print(Text(f"  {label_count}", style="bold"))
        style = CHANGE_STYLE[verb]
        if self.plain:
            for name in names:
                self.print(Text(f"    {name}", style=style))
            return
        shown, hidden = list(names), 0
        if not verbose and len(names) > LONG_LIST:
            shown, hidden = shown[:SHOWN_FIRST], len(names) - SHOWN_FIRST
        for row in _columns(shown, self.width - len(NAME_INDENT)):
            line = Text(NAME_INDENT)
            for at, (name, width) in enumerate(row):
                line.append(name, style=style)
                if at < len(row) - 1:
                    line.append(" " * (width - len(name) + NAME_GAP))
            self.print(line)
        if hidden:
            dots = "..." if self._ascii else "…"
            self.print(
                Text(
                    f"    {dots} and {hidden} more (--verbose shows them all)",
                    style="dim",
                )
            )

    def note(self, text: str) -> None:
        """A note after a result, behind a bullet."""
        line = Text("  ")
        line.append(self.glyph("bullet"), style=GLYPH_STYLE["bullet"])
        line.append(f" {text}")
        self.print(line)

    def warning(self, text: str) -> None:
        """A warning, on stderr: ``warning: <text>`` in the plain form, the
        warning glyph and the word ``warning`` before the text on a TTY."""
        if self.plain:
            self.print(f"warning: {text}", err=True)
            return
        line = Text()
        line.append(self.glyph("warning"), style=GLYPH_STYLE["warning"])
        line.append(" warning", style=GLYPH_STYLE["warning"])
        line.append(f"  {text}")
        self.print(line, err=True)

    def problems(self, heading: str, lines: Sequence[str]) -> None:
        """Problems, on stderr, each line as it is: under a red ``heading``
        on a TTY, alone in the plain form."""
        if not self.plain:
            self.heading("error", heading, err=True)
        for line in lines:
            self.print(line, err=True)


def _console(file: TextIO) -> Console:
    console = Console(
        file=file,
        markup=False,
        highlight=False,
        emoji=False,
        soft_wrap=True,
    )
    if console.no_color and console.color_system is not None:
        # NO_COLOR keeps the layout and drops every escape, bold and dim
        # included, not only the colours Rich strips on its own.
        console = Console(
            file=file,
            markup=False,
            highlight=False,
            emoji=False,
            soft_wrap=True,
            color_system=None,
        )
    return console


NAME_INDENT = "    "
NAME_GAP = 2


def _columns(names: Sequence[str], width: int) -> list[list[tuple[str, int]]]:
    """``names`` laid out column first in as many columns as ``width``
    holds, each column as wide as its longest name, two spaces apart: the
    rows, each a list of ``(name, its column's width)``."""
    for ncols in range(len(names), 0, -1):
        nrows = -(-len(names) // ncols)
        cols = [names[i : i + nrows] for i in range(0, len(names), nrows)]
        widths = [max(len(n) for n in col) for col in cols]
        if len(cols) == 1 or sum(widths) + NAME_GAP * (len(cols) - 1) <= width:
            return [
                [(col[r], widths[c]) for c, col in enumerate(cols) if r < len(col)]
                for r in range(nrows)
            ]
    return []


def count(n: int, noun: str) -> str:
    """``n`` and ``noun``, pluralised by an ``s`` unless ``n`` is one."""
    return f"{n} {noun}{'' if n == 1 else 's'}"


# -- new ------------------------------------------------------------------------


def print_new(
    out: Output, migration: GeneratedMigration, shown_dir: str, *, verbose: bool
) -> None:
    """What ``ferro migrate new`` wrote: the directory, each file with its
    role (and, under ``--replace``, what became of it), the change summary,
    the notes, and the warnings on stderr."""
    where = ""
    if migration.replaces is not None:
        where = (
            "(regenerated in place)"
            if migration.replaces == migration.dir_name
            else f"(replaces {migration.replaces}, now deleted)"
        )
    out.heading("ok", f"Wrote {shown_dir}", where)

    rows = list(_file_rows(migration))
    name_width = max(len(name) for name, *_ in rows)
    replaced = migration.replaces is not None
    role_width = max(len(role) for _, _, role, _ in rows)
    for name, kind, role, became in rows:
        line = Text("  ")
        line.append_text(out.file_name(name, kind=kind))
        line.append(" " * (name_width - len(name) + 2))
        line.append(role.ljust(role_width) if replaced else role, style="dim")
        if replaced:
            line.append(f"  {became}")
        out.print(line)

    if migration.summary or migration.notes:
        out.print()
    for change in migration.summary.changes:
        noun = f"{change.verb} {change.kind.removesuffix('s')}"
        out.names(
            count(len(change.names), noun),
            change.names,
            verb=change.verb,
            verbose=verbose,
        )
    for note in migration.notes:
        out.note(note)
    for warning in migration.warnings:
        out.warning(warning)


DATA_ROLE = {"backfill": "backfill", "guard": "guard", "data_step": "data step"}
"""A data step's role words, by the role the generator gave it; a carried
step, a person's whatever it began as, is a ``data step``."""


def _file_rows(migration: GeneratedMigration) -> Iterator[tuple[str, str, str, str]]:
    """Each file of ``migration``, ``ir.json`` last: its name, its kind (a
    key of :data:`KIND_STYLE`), its role words, and under ``--replace``
    what became of the replaced migration's file (else ``""``)."""
    from ..migrations.layout import SNAPSHOT_FILE

    step_of = {name: step for step in migration.steps for name in step.files}
    for name in migration.files():
        if name == SNAPSHOT_FILE:
            yield name, "snapshot", "snapshot", _became(migration, name)
            continue
        step = step_of[name]
        yield name, step.kind, _role(step, name), _became(migration, name)


def _role(step: GeneratedStep, name: str) -> str:
    if step.kind == "data":
        return DATA_ROLE.get(step.role or "data_step", "data step")
    direction = "down" if ".down." in name else "up"
    if step.kind == "portable_sql":
        return f"SQL step, {direction}"
    dialect = name.rsplit(".", 2)[-2]
    return f"schema step, {direction} ({dialect})"


def _became(migration: GeneratedMigration, name: str) -> str:
    """Under ``new --replace``, what became of the replaced migration's
    file now called ``name``: kept, moved, renumbered, carried (to review),
    new, or the new snapshot and what it chains from."""
    from ..migrations.layout import SNAPSHOT_FILE, Moved

    if migration.replaces is None:
        return ""
    if name == SNAPSHOT_FILE:
        parent = migration.chained_from or "no migration (the first)"
        return f"new snapshot, chained from {parent}"
    moved = {new: (old, how) for old, new, how in migration.moves}
    if name not in moved:
        return "new"
    old, how = moved[name]
    if how is Moved.CARRIED:
        over = any(
            step.over_namesake and name in step.files for step in migration.steps
        )
        wins = "; it wins over the regenerated step of the same name" if over else ""
        return f"carried, review it (was {old}{wins})"
    if how is Moved.RENUMBERED:
        return f"renumbered (was {old})"
    if migration.replaces == migration.dir_name:
        return "kept"
    return f"moved (was {old})"


# -- check ----------------------------------------------------------------------


def print_check(out: Output, report: CheckReport, *, verbose: bool) -> None:
    """What ``ferro migrate check`` found: ``ok: models match NNNN`` (the
    plain one-liner) or a heading on a TTY; else every problem on stderr as
    ``kind: message``, never collapsed. ``verbose`` is accepted for the
    shape every list-printing verb shares; no problem is ever hidden."""
    del verbose
    head = report.head or "no migration"
    if report.ok:
        if out.plain:
            out.print(f"ok: models match {head}")
        else:
            out.heading("ok", "models match", head)
        return
    out.problems(
        f"{count(len(report.problems), 'problem')} since {head}",
        [f"{problem.kind}: {problem.message}" for problem in report.problems],
    )
