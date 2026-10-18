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
list of the change summary collapsed behind ``--verbose``. ``NO_COLOR`` strips every escape
and keeps the layout; a dumb terminal gets the ASCII glyphs, and a stream
that cannot encode Unicode (``PYTHONIOENCODING=ascii``) gets ASCII
throughout: the glyphs, and ``-``, ``->`` and ``...`` for ``·``, ``→`` and
``…`` (:data:`ASCII_FORMS`); ``COLUMNS`` sets the width.

Words carry every meaning in both forms (a file's role, a list's count
heading, a problem's kind); colour only reinforces them. The tables below
are the one glyph table and the one colour table every verb reads.

The module reads report objects (:class:`GeneratedMigration`,
:class:`CheckReport`, :class:`StatusReport`, :class:`DriftReport`,
:class:`BaselineReport`, :class:`RerecordReport`, :class:`DownPlan`) and a run's
events (:class:`StepEvent`, :class:`RunNote`, a :class:`RunReport`'s
:class:`StepFailure`) and prints; it never changes them and never takes a
report's text apart.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, NamedTuple, TextIO

from datetime import UTC, datetime

from rich.console import Console
from rich.text import Text

if TYPE_CHECKING:
    from ..migrations._baseline import BaselineReport
    from ..migrations._drift import DriftReport
    from ..migrations.errors import MigrationRefused
    from ..migrations.generate import CheckReport
    from ..migrations.layout import Change
    from ..migrations.layout import GeneratedMigration, GeneratedStep
    from ..migrations.report import MigrationStatus, StatusReport
    from ..migrations.rerecord import RerecordReport
    from ..migrations.runner import (
        AdoptedEvent,
        AppliedStep,
        DownPlan,
        RunEvent,
        StepEvent,
    )

__all__ = [
    "CHANGE_STYLE",
    "GLYPHS",
    "KIND_STYLE",
    "STATE_GLYPH",
    "STATES",
    "STATE_STYLE",
    "Output",
    "StepFileRole",
    "applied_at_short",
    "kind_of_file",
    "print_adopted",
    "print_baseline",
    "print_changes",
    "print_check",
    "print_done",
    "print_down_plan",
    "print_drift",
    "print_event",
    "print_init",
    "print_new",
    "print_removed",
    "print_rerecord",
    "print_reverted",
    "print_run_failure",
    "print_status",
    "print_step",
    "step_file_role",
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

STATES: dict[str, tuple[str, str]] = {
    "applied": ("ok", "green"),
    "applied (baseline)": ("ok", "green"),
    "applied (adopted)": ("ok", "green"),
    "reverted": ("ok", "green"),
    "pending": ("pending", "yellow"),
    "pending (adoptable)": ("pending", "yellow"),
    "failed": ("error", "bold red"),
    "interrupted": ("error", "red"),
    "applied (different checksum)": ("error", "red"),
    "applied, not in the directory": ("error", "red"),
    "running": ("partial", "cyan"),
    "reverting": ("partial", "cyan"),
    "nothing to reverse": ("ok", "dim"),
    "partial": ("partial", "yellow"),
}
"""A migration's or a step's state word, as ``status``, ``up`` and ``down``
print it: the glyph its row opens with (a key of :data:`GLYPHS`) and its
style. ``partial`` stands for a migration's ``partial, X of N steps``, the
one state word that carries numbers. :data:`STATE_GLYPH` and
:data:`STATE_STYLE` are its two columns."""

STATE_GLYPH = {word: glyph for word, (glyph, _) in STATES.items()}
"""The glyph each state word's row opens with, from :data:`STATES`."""

STATE_STYLE = {word: style for word, (_, style) in STATES.items()}
"""Each state word's style, from :data:`STATES`."""

ASCII_FORMS = {"·": "-", "→": "->", "…": "..."}
"""What a stream that cannot encode Unicode prints for each such character
a line carries (the database line's separator, a rename's arrow, an
elision); the glyphs fall back through :data:`GLYPHS`."""

LONG_LIST = 12
"""A list of the change summary of up to this many names is shown whole on
a TTY."""
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
        self._ascii = self.plain or _ascii_only(out) or out.is_dumb_terminal

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

    def fit(self, text: str, *, err: bool = False) -> str:
        """``text`` as the stream can carry it: itself on a stream that
        encodes Unicode; else each :data:`ASCII_FORMS` character replaced,
        and any other character the stream cannot encode (a name's accent)
        written as its backslash escape (``\\xe9``), so nothing is lost and
        nothing raises."""
        console = self.err if err else self.out
        if not _ascii_only(console):
            return text
        for char, form in ASCII_FORMS.items():
            text = text.replace(char, form)
        encoding = console.encoding
        return text.encode(encoding, "backslashreplace").decode(encoding)

    def print(self, text: str | Text = "", *, err: bool = False) -> None:
        """Print one line, through :meth:`fit`. No form wraps it: a script
        reads what was said, and a command quoted in a note or a refusal is
        copied whole; a terminal folds a long line on its own.

        A stream that is not a terminal gets the line's text written to it
        as it is, never laid out by Rich: no width (``COLUMNS=0`` included)
        decides whether a line prints, and a tab or a carriage return in a
        refusal reaches the pipe unchanged."""
        console = self.err if err else self.out
        if not console.is_terminal:
            line = text if isinstance(text, str) else text.plain
            console.file.write(self.fit(line, err=err) + "\n")
            console.file.flush()
            return
        if _ascii_only(console):
            text = (
                self.fit(text, err=err)
                if isinstance(text, str)
                else self._fit_text(text, err=err)
            )
        console.print(text)

    def _fit_text(self, text: Text, *, err: bool) -> Text:
        """:meth:`fit` over a styled line, each run keeping its styles."""
        cuts = sorted(
            {span.start for span in text.spans} | {span.end for span in text.spans}
        )
        fitted = Text(style=text.style, end=text.end)
        for piece in text.divide(cuts):
            part = Text(self.fit(piece.plain, err=err))
            for span in piece.spans:
                part.stylize(span.style)
            fitted.append_text(part)
        return fitted

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
        what it touches, dim: ``main · postgresql://pinch:***@db:5432/app``
        (``where`` the masked URL), or ``main · migrations/main`` for
        ``new`` and ``check``, which never connect (``where`` the
        directory)."""
        text = Text(f"{name} · {where}")
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
        err: bool = False,
    ) -> None:
        """One list of the change summary: its count heading (``2 new models``), plain
        bold, then its names coloured by ``verb``. One name per line in the
        plain form; columns on a TTY, where a list longer than
        :data:`LONG_LIST` shows its first :data:`SHOWN_FIRST` unless
        ``verbose``."""
        self.print(Text(f"  {label_count}", style="bold"), err=err)
        style = CHANGE_STYLE[verb]
        names = [self.fit(name, err=err) for name in names]
        if self.plain:
            for name in names:
                self.print(Text(f"    {name}", style=style), err=err)
            return
        shown, hidden = list(names), 0
        if not verbose and len(names) > LONG_LIST:
            shown, hidden = shown[:SHOWN_FIRST], len(names) - SHOWN_FIRST
        width = (self.err if err else self.out).width
        for row in _columns(shown, width - len(NAME_INDENT)):
            line = Text(NAME_INDENT)
            for at, (name, column) in enumerate(row):
                line.append(name, style=style)
                if at < len(row) - 1:
                    line.append(" " * (column - len(name) + NAME_GAP))
            self.print(line, err=err)
        if hidden:
            dots = "..." if self._ascii else "…"
            self.print(
                Text(
                    f"    {dots} and {hidden} more (--verbose shows them all)",
                    style="dim",
                ),
                err=err,
            )

    def note(self, text: str, *, err: bool = False) -> None:
        """A note after a result, behind a bullet."""
        line = Text("  ")
        line.append(self.glyph("bullet"), style=GLYPH_STYLE["bullet"])
        line.append(f" {text}")
        self.print(line, err=err)

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

    def refusal(self, heading: str, sentence: str) -> None:
        """A verb's refusal, on stderr: the sentence as it is, under a red
        ``heading`` naming what refused (``drift refused``) on a TTY, alone
        in the plain form. The one refusal path every verb prints through."""
        self.problems(heading, [sentence])


def _ascii_only(console: Console) -> bool:
    """The one answer to "ASCII only?": the console's stream cannot encode
    Unicode, by the reading Rich itself uses for its box characters."""
    return console.options.ascii_only


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
    if console.width < 1:
        # A width below one (``COLUMNS=0``, a pseudo-terminal reporting no
        # size) is no width at all: Rich would render nothing. Lay out at
        # the width Rich assumes when it cannot tell.
        console.width = FALLBACK_WIDTH
    return console


FALLBACK_WIDTH = 80
"""The terminal width the TTY form lays out at when the reported one is
below one column."""


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
    if migration.squash:
        first, last = migration.replaces[0], migration.replaces[-1]
        run = first if first == last else f"{first} … {last}"
        where = f"(replaces {run}, now deleted)"
    elif migration.replaces:
        [replaced_dir] = migration.replaces
        where = (
            "(regenerated in place)"
            if replaced_dir == migration.dir_name
            else f"(replaces {replaced_dir}, now deleted)"
        )
    out.heading("ok", f"Wrote {shown_dir}", where)

    rows = list(_file_rows(migration))
    name_width = max(len(name) for name, *_ in rows)
    replaced = bool(migration.replaces)
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
    print_changes(out, migration.summary.changes, verbose=verbose)
    for note in migration.notes:
        out.note(note)
    for warning in migration.warnings:
        out.warning(warning)


def print_changes(
    out: Output, changes: Sequence[Change], *, verbose: bool, err: bool = False
) -> None:
    """A change summary laid out from its data, as ``new`` and ``check``
    print it: each change's count heading (``2 new models``), then its
    names (:meth:`Output.names`)."""
    for change in changes:
        noun = f"{change.verb} {change.kind.removesuffix('s')}"
        out.names(
            count(len(change.names), noun),
            change.names,
            verb=change.verb,
            verbose=verbose,
            err=err,
        )


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
    """A file's role words: a data step's by the role the generator gave
    it; a SQL file's by its direction and dialect, as the core reads its
    name (:func:`step_file_role`)."""
    if step.kind == "data":
        return DATA_ROLE.get(step.role or "data_step", "data step")
    role = step_file_role(name)
    if step.kind == "portable_sql":
        return f"SQL step, {role.direction}"
    return f"schema step, {role.direction} ({role.dialect})"


def _became(migration: GeneratedMigration, name: str) -> str:
    """Under ``new --replace``, what became of the replaced migration's
    file now called ``name``: kept, moved, renumbered, carried (to review),
    new, or the new snapshot and what it chains from. Under ``squash``, a
    generated file is regenerated, a carried one names the original it was,
    and the snapshot is the run's last one's, composed."""
    from ..migrations.layout import SNAPSHOT_FILE, Moved

    if not migration.replaces:
        return ""
    if name == SNAPSHOT_FILE:
        parent = migration.chained_from or "no migration (the first)"
        if migration.squash:
            first, last = migration.replaces[0], migration.replaces[-1]
            run = first if first == last else f"{first}…{last}"
            return f"{last}'s, chained from {parent}, replaces {run}"
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
    if how is Moved.REGENERATED:
        return "regenerated"
    if how is Moved.RENUMBERED:
        return f"renumbered (was {old})"
    if migration.replaces == [migration.dir_name]:
        return "kept"
    return f"moved (was {old})"


# -- check ----------------------------------------------------------------------


def print_check(out: Output, report: CheckReport, *, verbose: bool) -> None:
    """What ``ferro migrate check`` found: ``ok: models match NNNN`` (the
    plain one-liner) or a heading on a TTY; else every problem on stderr,
    never dropped. In the plain form each problem is ``kind: message``, the
    change summary inside the sentence as the core wrote it. On a TTY the
    problems sit under a red heading, and an ``ungenerated`` problem prints
    its sentence without the summary, then the summary laid out from its
    data as ``new`` lays it out (:func:`print_changes`: a long list
    collapses unless ``verbose``), then its notes."""
    head = report.head or "no migration"
    if report.ok:
        if out.plain:
            out.print(f"ok: models match {head}")
        else:
            out.heading("ok", "models match", head)
        return
    if out.plain:
        for problem in report.problems:
            out.print(f"{problem.kind}: {problem.message}", err=True)
        return
    out.heading(
        "error", f"{count(len(report.problems), 'problem')} since {head}", err=True
    )
    for problem in report.problems:
        if problem.headline is None:
            out.print(f"{problem.kind}: {problem.message}", err=True)
            continue
        out.print(f"{problem.kind}: {problem.headline}", err=True)
        print_changes(out, problem.changes, verbose=verbose, err=True)
        for note in problem.notes:
            out.note(note, err=True)


# -- status ---------------------------------------------------------------------


def applied_at_short(iso: str | None) -> str:
    """A stored finish time to the minute in UTC with an explicit ``Z``
    (``2026-10-14T09:40:12.123Z`` is ``2026-10-14 09:40Z``); ``""`` for
    ``None``."""
    if iso is None:
        return ""
    return datetime.fromisoformat(iso).astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


class StepFileRole(NamedTuple):
    """What a step file's name says it is, as the core reads it."""

    kind: str
    """``ddl``, ``data`` or ``portable_sql``."""
    direction: str
    """``up`` or ``down``."""
    dialect: str
    """``postgres``, ``sqlite`` or ``portable``."""


def step_file_role(name: str) -> StepFileRole:
    """The kind, direction and dialect a step file's name says, read by the
    directory reader's own parse (``_core._step_file_role``); ``ValueError``
    for a name that is not a step file's."""
    from .._core import _step_file_role

    return StepFileRole(**json.loads(_step_file_role(name)))


def kind_of_file(name: str) -> str:
    """A step file's kind (a key of :data:`KIND_STYLE`): the core's reading
    of its name (:func:`step_file_role`), or ``snapshot`` for ``ir.json``;
    ``ValueError`` for any other name."""
    from ..migrations.layout import SNAPSHOT_FILE

    if name == SNAPSHOT_FILE:
        return "snapshot"
    return step_file_role(name).kind


def _state_key(word: str) -> str:
    """The key of :data:`STATE_STYLE` and :data:`STATE_GLYPH` for a state
    word: itself, or ``partial`` for ``partial, X of N steps``."""
    return word if word in STATE_STYLE else "partial"


def _status_title(report: StatusReport) -> tuple[str, str]:
    """The heading's glyph key and its counts (``1 failed · 2 pending · 1
    ahead``): the migrations with a step in each unfinished state, with an
    edited applied step, with a step still to apply, and the applied
    migrations the directory lacks. A refusal with nothing to count:
    ``needs attention``. Everything applied: ``N applied``."""

    def having(state: str) -> int:
        return sum(
            1 for m in report.migrations if any(s.state == state for s in m.steps)
        )

    counts = [
        (having("failed"), "failed"),
        (having("interrupted"), "interrupted"),
        (having("reverting"), "reverting"),
        (having("running"), "running"),
        (having("applied (different checksum)"), "edited"),
        (sum(1 for m in report.migrations if m.pending), "pending"),
        (len(report.ahead), "ahead"),
    ]
    title = " · ".join(f"{n} {word}" for n, word in counts if n)
    if report.needs_attention or (report.refusal is not None and not title):
        # A refusal with nothing to count (a directory status cannot read,
        # a newer tracking format) still heads the report.
        return "warning", title or "needs attention"
    if report.pending:
        return "pending", title
    return "ok", f"{len(report.migrations)} applied"


def print_status(out: Output, report: StatusReport, *, steps: bool) -> None:
    """Where the database stands: a heading counting what is failed,
    pending and ahead beside the tracking table; one row per migration
    (glyph, name, state word, ``applied_at``), never collapsed; the steps
    of a migration that needs attention, or of every one with ``steps``;
    the migrations applied but not in the directory; and what ``up`` would
    refuse with, under ``needs attention``. All of it on stdout: the
    refusal is part of the report."""
    key, title = _status_title(report)
    out.heading(key, title, report.table)
    names = [m.name for m in report.migrations] + report.ahead
    width = max((len(name) for name in names), default=0)
    for migration in report.migrations:
        _status_row(
            out,
            migration.name,
            migration.state,
            width,
            migration.applied_at,
            hint=migration.hint,
        )
        if steps or migration.expands:
            _status_steps(out, migration, steps=steps)
    for name in report.ahead:
        _status_row(out, name, "applied, not in the directory", width, None)
    if report.refusal is not None:
        out.print()
        out.heading(
            "error" if report.refusal_needs_attention else "warning",
            "needs attention",
        )
        for line in report.refusal.splitlines():
            out.print(f"  {line}")


def _status_row(
    out: Output,
    name: str,
    state: str,
    width: int,
    applied_at: str | None,
    *,
    hint: str | None = None,
) -> None:
    """One migration's row: glyph, name, state word, then its ``applied_at``
    dim, or the migration's hint plain (a squash pending adoption names
    ``ferro migrate up``)."""
    key = _state_key(state)
    glyph = STATE_GLYPH[key]
    line = Text("  ")
    line.append(out.glyph(glyph), style=GLYPH_STYLE[glyph])
    line.append(f" {name:<{width}}  ")
    line.append(state, style=STATE_STYLE[key])
    when = applied_at_short(applied_at)
    if when:
        line.append(f"  {when}", style="dim")
    if hint:
        line.append(f"  {hint}")
    out.print(line)


def _status_steps(out: Output, migration: MigrationStatus, *, steps: bool) -> None:
    """A migration's steps, six spaces in: each file coloured by its kind,
    its state, ``[flag]``s and (with ``--steps``) its class; beneath, an
    edited step's two checksums or an unfinished step's error, where
    :meth:`StatusReport.render` shows them."""
    width = max((len(step.file) for step in migration.steps), default=0)
    for step in migration.steps:
        line = Text("      ")
        line.append_text(out.file_name(step.file, kind=kind_of_file(step.file)))
        line.append(" " * (width - len(step.file) + 2))
        line.append(step.shown, style=STATE_STYLE[_state_key(step.state)])
        for flag in step.flags:
            line.append(f"  [{flag}]", style="bold yellow")
        if steps:
            line.append(f"  {step.step_class}", style="dim")
        out.print(line)
        if step.state == "applied (different checksum)":
            for label, checksum in (
                ("applied", step.applied_checksum),
                ("on disk", step.on_disk_checksum),
            ):
                out.print(Text(f"        {label}  sha384:{checksum}", style="dim"))
        elif step.error and step.state in ("failed", "running", "reverting"):
            for error in step.error.splitlines():
                out.print(Text(f"        {error}", style="red"))


# -- drift ----------------------------------------------------------------------


def print_drift(out: Output, report: DriftReport) -> None:
    """What ``ferro migrate drift`` found. Its warnings first, on stderr;
    then, when it refused, the refusal sentence on stderr as it is (under a
    red ``drift refused`` heading on a TTY, :meth:`Output.refusal`); else
    ``no drift against NNNN`` (the plain one-liner, an ok heading on a TTY)
    or ``N differences against NNNN`` and one bullet per finding, never
    collapsed."""
    for warning in report.warnings:
        out.warning(warning)
    if report.refusal is not None:
        out.refusal("drift refused", report.refusal)
        return
    if report.clean:
        if out.plain:
            out.print(f"no drift against {report.against}")
        else:
            out.heading(
                "ok", Text("no drift", style="bold green"), f"against {report.against}"
            )
        return
    _drift_findings(out, report)


def _drift_findings(out: Output, report: DriftReport) -> None:
    """A drift report's findings: the red count heading, one bullet each."""
    out.heading(
        "error", f"{count(len(report.lines), 'difference')} against {report.against}"
    )
    for finding in report.lines:
        line = Text("  ")
        line.append(out.glyph("bullet"), style="red")
        line.append(f" {finding}")
        out.print(line)


# -- up and down ----------------------------------------------------------------


def print_step(
    out: Output, event: StepEvent, *, name_width: int, stem_width: int
) -> None:
    """One step a run finished, as it finishes. The plain form is the line
    ``progress`` receives (:meth:`StepEvent.line`); on a TTY a row two
    spaces in: the glyph, the migration, the step coloured by its kind, the
    state word and the duration in seconds, dim (``nothing to reverse``
    gives its reason instead). A failed step is a red row on a TTY and
    nothing in the plain form, where its message alone says it
    (:func:`print_run_failure`)."""
    if out.plain:
        line = event.line(name_width, stem_width)
        if line is not None:
            out.print(line)
        return
    glyph = STATE_GLYPH[event.outcome]
    line = Text("  ")
    line.append(out.glyph(glyph), style=GLYPH_STYLE[glyph])
    line.append(f" {event.migration:<{name_width}}  ")
    line.append(event.stem, style=KIND_STYLE[kind_of_file(event.file)])
    line.append(" " * (stem_width - len(event.stem) + 2))
    line.append(event.outcome, style=STATE_STYLE[event.outcome])
    if event.duration_ms is not None:
        line.append(f"  {event.duration_ms / 1000:.1f}s", style="dim")
    if event.reason is not None:
        line.append(f": {event.reason}", style="dim")
    out.print(line)


def print_run_failure(out: Output, refused: MigrationRefused) -> None:
    """Why ``up`` or ``down`` stopped, on stderr: its report's refusal (or,
    with no run report, the exception's text) as it is in the plain form.
    On a TTY a blank line, then a failed step under a red ``<step>
    failed`` heading (``after N rows`` for a chunked step's committed
    batches) with its error red and its hint plain, indented two; any other
    refusal under ``up refused`` / ``down refused``."""
    from ..migrations.runner import RunReport

    report = refused.report
    if not isinstance(report, RunReport) or report.refusal is None:
        out.refusal("refused", str(refused))
        return
    failed = report.failed
    if out.plain or failed is None:
        out.refusal(f"{report.direction} refused", report.refusal)
        return
    out.print(err=True)
    where = "" if failed.rows_done is None else f"after {failed.rows_done:,} rows"
    out.heading("error", f"{failed.stem} failed", where, err=True)
    for line in failed.error.splitlines():
        out.print(Text(f"  {line}", style="red"), err=True)
    for line in failed.hint.splitlines():
        out.print(f"  {line}", err=True)


def print_down_plan(out: Output, plan: DownPlan) -> None:
    """What ``down`` would revert, newest first: :meth:`DownPlan.describe`
    in the plain form; on a TTY a warning heading naming the migrations and
    the step count, then a row per step, its stem coloured by kind and a
    nothing-to-reverse reason dim."""
    if out.plain:
        out.print(plan.describe())
        return
    migrations = list(dict.fromkeys(step.migration for step in plan.steps))
    out.heading(
        "warning",
        "down reverts, newest first",
        f"{', '.join(migrations)}, {count(len(plan.steps), 'step')}",
    )
    name_width = max(len(step.migration) for step in plan.steps)
    stem_width = max(len(step.step) for step in plan.steps)
    for step in plan.steps:
        line = Text(f"  {step.migration:<{name_width}}  ")
        line.append(step.step, style=KIND_STYLE[kind_of_file(step.file)])
        if step.nothing_to_reverse is not None:
            line.append(" " * (stem_width - len(step.step)))
            line.append(f"  nothing to reverse: {step.nothing_to_reverse}", style="dim")
        out.print(line)


def print_reverted(out: Output, reverted: Sequence[AppliedStep]) -> None:
    """A finished ``down``'s closing heading, on a TTY only: ``reverted
    <migrations>  N steps``. Where the database now stands is ``status``'s
    to say: neither the plan nor the run report names the migration below
    the lowest step reverted."""
    if out.plain:
        return
    migrations = list(dict.fromkeys(step.migration for step in reverted))
    out.heading(
        "ok",
        f"reverted {', '.join(migrations)}",
        count(len(reverted), "step"),
    )


def print_adopted(out: Output, event: AdoptedEvent) -> None:
    """A squash the run adopted, before any step (ADR-0056): the line
    ``progress`` receives in the plain form (``adopted 0005_teams_epic: this
    database had applied 0005_add_teams … 0009_fix_rows; nothing was
    run``); on a TTY an ok heading, ``adopted <squash>`` bold and the rest
    dim."""
    from ..migrations.report import migration_span

    if out.plain:
        out.print(event.line())
        return
    out.heading(
        "ok",
        f"adopted {event.squash}",
        f"this database had applied {migration_span(event.replaced)}; nothing was run",
    )


def print_event(out: Output, event: RunEvent) -> None:
    """One line a run says, as it says it: a finished step through
    :func:`print_step` at the run's widths, an adopted squash through
    :func:`print_adopted`; any other line as it is."""
    from ..migrations.runner import AdoptedEvent, StepEvent

    if isinstance(event, StepEvent):
        print_step(out, event, name_width=event.name_width, stem_width=event.stem_width)
    elif isinstance(event, AdoptedEvent):
        print_adopted(out, event)
    else:
        out.print(event.text)


def print_done(out: Output, title: str, where: str) -> None:
    """A run with nothing to do: ``<title>: <where>`` in the plain form
    (``nothing to apply: the database is up to date``), an ok heading on a
    TTY; ``title`` alone when ``where`` is empty (``nothing to revert``)."""
    if out.plain:
        out.print(f"{title}: {where}" if where else title)
    else:
        out.heading("ok", title, where)


# -- baseline -------------------------------------------------------------------


def print_baseline(out: Output, report: BaselineReport) -> None:
    """What ``ferro migrate baseline`` did. Its warnings on stderr first.
    The plain form is :meth:`BaselineReport.render`. On a TTY: the drift it
    found (and that nothing was recorded); or an ok heading with the step
    count, one line per data step it listed without running (its file
    coloured by kind), and the not-compared note, dim."""
    from ..migrations._baseline import NOT_COMPARED, _span

    for warning in report.warnings:
        out.warning(warning)
    if out.plain:
        out.print(report.render())
        return
    if report.drift is not None:
        _drift_findings(out, report.drift)
        out.print(Text("nothing was recorded", style="bold"))
        return
    where = count(report.steps, "step")
    if report.data_steps_listed:
        where += f"; {count(len(report.data_steps_listed), 'data step')} listed"
    out.heading("ok", f"recorded {_span(report.recorded)} as baseline", where)
    for listed in report.data_steps_listed:
        migration, _, file = listed.rpartition("/")
        line = Text(f"  {migration}/")
        line.append_text(out.file_name(file, kind=kind_of_file(file)))
        line.append("  recorded, not run", style="dim")
        out.print(line)
    out.print(Text(f"  {NOT_COMPARED}", style="dim"))


def print_removed(out: Output, removed: list[str]) -> None:
    """What ``baseline --remove`` removed: :func:`render_removed` in the
    plain form, under an ok glyph on a TTY."""
    from ..migrations._baseline import render_removed

    if out.plain:
        out.print(render_removed(removed))
    else:
        out.heading("ok", render_removed(removed))


# -- rerecord -------------------------------------------------------------------


def print_rerecord(out: Output, report: RerecordReport) -> None:
    """What ``ferro migrate rerecord`` changed: :meth:`RerecordReport.render`
    in the plain form; on a TTY an ok heading naming the file (coloured by
    kind) and that nothing was run, the two checksums with an arrow, and
    what the mode leaves the next ``up``, dim."""
    if out.plain:
        out.print(report.render())
        return
    migration, _, file = report.file.rpartition("/")
    title = Text(f"re-recorded {migration}/", style="bold")
    title.append_text(out.file_name(file, kind=kind_of_file(file)))
    out.heading("ok", title, "nothing was run")
    line = Text(f"  sha384:{report.old_checksum} ")
    line.append(out.glyph("arrow"), style=GLYPH_STYLE["arrow"])
    line.append(f" sha384:{report.new_checksum}")
    out.print(line)
    if report.note is not None:
        out.print(Text(f"  {report.note}", style="dim"))


# -- init -----------------------------------------------------------------------


def print_init(out: Output, block: str, written: str, created: str) -> None:
    """What ``ferro migrate init`` wrote: the config ``block`` as written,
    then ``Wrote <written> and created <created>`` in the plain form; on a
    TTY the block dim, then an ok heading."""
    for line in block.splitlines():
        out.print(Text(line, style="dim") if not out.plain else line)
    if out.plain:
        out.print(f"Wrote {written} and created {created}")
    else:
        out.heading("ok", f"Wrote {written}", f"created {created}")
