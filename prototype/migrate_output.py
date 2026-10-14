# /// script
# dependencies = ["rich>=14"]
# ///
"""PROTOTYPE, throwaway (#646): what should `ferro migrate` output look like?

Three structurally different presentations of every verb's result, over
fixed sample data, plus today's output (variant 0) for comparison::

    uv run prototype/migrate_output.py                 # variant A, every verb
    uv run prototype/migrate_output.py --variant B     # B or C, or 0 for today
    uv run prototype/migrate_output.py --verb status   # one verb
    uv run prototype/migrate_output.py --plain         # stdout is not a TTY
    uv run prototype/migrate_output.py --verbose       # long lists in full
    uv run prototype/migrate_output.py --html out.html # every variant, one page

Nothing here touches a database or the real CLI. The sample data is the
pinch-backend run that opened #646 plus one small follow-up migration, a
status with every state, a drift with every kind of finding, and one failed
up. Each variant is a set of functions, one per verb, over the same data.

Variants:

  0  Today      what the CLI prints now, verbatim shapes.
  A  Headed     a glyph-and-heading per result, details indented beneath it,
                name lists in wrapped columns, counts in the summary.
  B  Tabular    every result is a table with a header rule: files, changes,
                migrations, findings, steps. Dense, column-aligned.
  C  Gutter     cargo/uv style: a right-aligned coloured verb in a fixed
                gutter, one sentence per line, lists wrapped with a hanging
                indent. No tables, no boxes.

--plain shows what a pipe or CI log gets: no colour, ASCII glyphs, and the
one-line forms tooling greps today kept byte for byte.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from dataclasses import dataclass, field

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.table import Table
from rich.terminal_theme import TerminalTheme
from rich.text import Text

TERMINAL = TerminalTheme(
    (17, 19, 26),
    (222, 226, 232),
    [(70, 76, 90), (255, 110, 110), (104, 216, 140), (240, 200, 90), (110, 170, 255), (210, 140, 255), (90, 215, 220), (222, 226, 232)],
    [(110, 116, 130), (255, 140, 140), (140, 230, 170), (250, 215, 120), (140, 190, 255), (225, 170, 255), (130, 230, 235), (255, 255, 255)],
)

# -- sample data ---------------------------------------------------------------

PINCH_MODELS = (
    "Account AuthAttempt BalanceEntry Category Connection Conversation "
    "CorrectionLogEntry EmailVerificationToken Enrollment FactorClearance Holding "
    "Import ImportProfile ImportRow InvestmentActivity Invitation Ledger "
    "LedgerMember LegalVersions Merchant MerchantIdentityKey MergeAbsorption "
    "MergeEvent MfaChallenge PasswordResetToken PersonalAccessToken "
    "PolicyAcceptance Proposal ProposalTag ProviderRecord RecoveryCode "
    "RecurringSeries Rule Security Session SplitLine SuggestionDismissal Tag "
    "TotpFactor Transaction TransactionTag Transfer User WebauthnChallenge "
    "WebauthnCredential"
).split()

PINCH_ENUMS = (
    "accountkind balancesource categorycolor connectionprovider connectionstatus "
    "correctionactor correctionkind farsideclaim importstatus "
    "investmentactivitytype ledgerrole merchantsource patscope proposalprovenance "
    "providerrecorddelivery providerrecordkind recurringcadence recurringkind "
    "recurringstatus rulekind ruleorigin rulestatus webauthnchallengepurpose "
    "webauthncredentialdevicetype"
).split()


@dataclass
class File:
    name: str
    role: str  # "schema step", "backfill", "guard", "snapshot"
    direction: str = ""  # "up" / "down" / ""
    dialect: str = ""  # "postgres" / "sqlite" / ""


@dataclass
class NewMigration:
    argv: str
    directory: str
    files: list[File]
    changes: dict[str, list[str]]  # label -> names, in the summary's order
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


NEW_INITIAL = NewMigration(
    argv="ferro migrate new initial",
    directory="migrations/0001_initial/",
    files=[
        File("01_schema.up.postgres.sql", "schema step", "up", "postgres"),
        File("01_schema.down.postgres.sql", "schema step", "down", "postgres"),
        File("ir.json", "snapshot"),
    ],
    changes={"new models": PINCH_MODELS, "new enum types": PINCH_ENUMS},
    warnings=[
        'row security on "ledger_member" is Postgres-only; SQLite gets no policy'
    ],
)

NEW_TEAM = NewMigration(
    argv="ferro migrate new add_team --data-step Transaction",
    directory="migrations/0002_add_team/",
    files=[
        File("01_expand.up.postgres.sql", "schema step", "up", "postgres"),
        File("01_expand.down.postgres.sql", "schema step", "down", "postgres"),
        File("01_expand.up.sqlite.sql", "schema step", "up", "sqlite"),
        File("01_expand.down.sqlite.sql", "schema step", "down", "sqlite"),
        File("02_backfill_account.py", "backfill"),
        File("03_backfill_transaction.py", "data step"),
        File("04_contract.up.postgres.sql", "schema step", "up", "postgres"),
        File("04_contract.down.postgres.sql", "schema step", "down", "postgres"),
        File("04_contract.up.sqlite.sql", "schema step", "up", "sqlite"),
        File("04_contract.down.sqlite.sql", "schema step", "down", "sqlite"),
        File("ir.json", "snapshot"),
    ],
    changes={
        "new models": ["Team", "TeamMember"],
        "changed models": ["Account", "Transaction", "User"],
        "new enum labels": ["accountkind.escrow"],
    },
    notes=[
        "02_backfill_account.py: fill account.team_id for every row, then "
        "04_contract makes it NOT NULL",
        "03_backfill_transaction.py: write the todo(...) before `ferro migrate up`",
    ],
)

CHECK_OK = {"argv": "ferro migrate check", "head": "0002_add_team"}
CHECK_BAD = {
    "argv": "ferro migrate check",
    "problems": [
        (
            "ungenerated",
            "the models changed since 0002_add_team: new models: Invoice; "
            "changed models: Account",
        ),
        (
            "todo",
            "migrations/0002_add_team/03_backfill_transaction.py still holds "
            "todo(...)",
        ),
    ],
}

UP_RUN = {
    "argv": "ferro migrate up",
    "database": "main",
    "dialect": "postgres",
    "steps": [
        ("0002_add_team", "01_expand", "applied", 412, None),
        ("0002_add_team", "02_backfill_account", "applied", 8_210, None),
        (
            "0002_add_team",
            "03_backfill_transaction",
            "failed",
            31_904,
            (
                "asyncpg.exceptions.NotNullViolationError: null value in column "
                '"team_id" of relation "transaction" violates not-null constraint\n'
                "DETAIL:  Failing row contains (184021, null, 2026-03-01, ...)."
            ),
        ),
    ],
    "rows_done": 30_000,
    "hint": (
        "The failing batch was rolled back; the 30,000 rows of the batches "
        "before it stay committed. Fix the file or the database and run "
        "`ferro migrate up` again to resume after them."
    ),
}

DOWN_RUN = {
    "argv": "ferro migrate down --to 0001",
    "plan": [
        ("0002_add_team", "04_contract", None),
        ("0002_add_team", "03_backfill_transaction", "a data step with no down"),
        ("0002_add_team", "02_backfill_account", None),
        ("0002_add_team", "01_expand", None),
    ],
    "question": "Revert 0002_add_team (4 steps)?",
    "results": [
        ("0002_add_team", "04_contract", "reverted", 98),
        ("0002_add_team", "03_backfill_transaction", "nothing to reverse", 0),
        ("0002_add_team", "02_backfill_account", "reverted", 1_204),
        ("0002_add_team", "01_expand", "reverted", 310),
    ],
}


@dataclass
class Step:
    file: str
    state: str
    step_class: str = "contract"
    flags: list[str] = field(default_factory=list)
    rows_done: int | None = None
    error: str | None = None
    applied_checksum: str | None = None
    on_disk_checksum: str | None = None


@dataclass
class Migration:
    name: str
    state: str
    steps: list[Step]
    applied_at: str | None = None

    @property
    def expands(self) -> bool:
        return self.state not in ("applied", "pending", "applied (baseline)")


STATUS = {
    "argv": "ferro migrate status",
    "database": "main",
    "dialect": "postgres",
    "table": "_ferro_migrations",
    "migrations": [
        Migration(
            "0001_initial",
            "applied (baseline)",
            [Step("01_schema.up.postgres.sql", "applied (baseline)", "expand")],
            "2026-09-02 14:11",
        ),
        Migration(
            "0002_add_team",
            "partial, 2 of 4 steps",
            [
                Step("01_expand.up.postgres.sql", "applied", "expand"),
                Step("02_backfill_account.py", "applied", "expand"),
                Step(
                    "03_backfill_transaction.py",
                    "failed",
                    "expand",
                    rows_done=30_000,
                    error=(
                        "asyncpg.exceptions.NotNullViolationError: null value in "
                        'column "team_id" of relation "transaction" violates '
                        "not-null constraint"
                    ),
                ),
                Step("04_contract.up.postgres.sql", "pending", "contract"),
            ],
            "2026-10-14 09:40",
        ),
        Migration(
            "0003_drop_legacy",
            "pending",
            [
                Step(
                    "01_schema.up.postgres.sql",
                    "pending",
                    "contract",
                    flags=["destructive"],
                )
            ],
        ),
        Migration(
            "0004_author_slug",
            "applied (different checksum)",
            [
                Step(
                    "01_schema.up.postgres.sql",
                    "applied (different checksum)",
                    "expand",
                    applied_checksum="3f9a…c21e",
                    on_disk_checksum="88b0…14d7",
                )
            ],
            "2026-10-13 18:02",
        ),
    ],
    "ahead": ["0005_hotfix"],
    "refusal": (
        "0005_hotfix is applied but not in the directory: restore the file, or "
        "pass allow_ahead= to the application's up()"
    ),
}

DRIFT = {
    "argv": "ferro migrate drift",
    "against": "0002_add_team",
    "lines": [
        ("team", "column", "team.name column is missing"),
        ("team", "column", "team.slug column is extra"),
        ("account", "index", "idx_account_team_id index is missing"),
        ("account", "check", "ck_account_balance check body differs from the snapshot"),
        ("transaction", "default", "transaction.posted_at has default now() in the database, none declared"),
        ("accountkind", "enum", "accountkind enum type is missing label escrow"),
    ],
    "warnings": ["accountkind enum type has label legacy the snapshot never declared"],
}

BASELINE = {
    "argv": "ferro migrate baseline 0002",
    "recorded": ["0001_initial", "0002_add_team"],
    "steps": 5,
    "data_steps": ["0002_add_team/03_backfill_transaction.py"],
    "not_compared": (
        "baseline checked what `ferro migrate drift` checks: objects ferro does "
        "not own (functions, triggers) were not compared"
    ),
}

RERECORD = {
    "argv": "ferro migrate rerecord 0002:03 --continue",
    "file": "0002_add_team/03_backfill_transaction.py",
    "old": "3f9a…c21e",
    "new": "88b0…14d7",
    "mode": "continue",
}

INIT = {
    "argv": "ferro migrate init",
    "block": (
        "[tool.ferro]\n"
        'models = ["pinch.models"]\n'
        'dialects = ["postgres"]\n'
        'directory = "migrations"\n'
    ),
    "written": "[tool.ferro] to pyproject.toml",
    "created": "migrations/",
}

VERBS = ["init", "new", "check", "up", "down", "status", "drift", "baseline", "rerecord"]

# Which database a verb worked on. Every verb's first line names it: the
# config entry, and the URL with its password replaced (new and check read
# models and files only, so they name the directory instead).
DB = {
    "name": "main",
    "url": "postgresql://pinch:hunter2@db.internal.pinch.finance:5432/pinch_prod?sslmode=require",
    "directory": "migrations/main",
}
CLEAN_DB_URL = "sqlite:///./dev.db"


def redact(url: str) -> str:
    """The URL with its password replaced by ***; everything else as given."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    netloc = f"{parts.username}:***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

# -- shared bits ---------------------------------------------------------------

GLYPH = {  # (tty, plain)
    "ok": ("✔", "ok"),
    "warn": ("▲", "!!"),
    "err": ("✘", "xx"),
    "pending": ("○", "--"),
    "partial": ("◐", "~~"),
    "bullet": ("•", "-"),
    "arrow": ("→", "->"),
}
STATE_STYLE = {
    "applied": "green",
    "applied (baseline)": "green",
    "pending": "yellow",
    "failed": "bold red",
    "running": "cyan",
    "reverting": "cyan",
    "interrupted": "red",
    "applied (different checksum)": "red",
    "reverted": "green",
    "nothing to reverse": "dim",
}

# One colour per kind of file, as `GeneratedStep.kind` names them: ddl (a
# schema step the generator rendered, one per dialect), portable_sql (a
# hand-written .up.sql / .down.sql), data (a Python step: a generated
# backfill or a --data-step), and the snapshot. The suffix stays dim so the
# eye lands on the step's name.
KIND_STYLE = {"ddl": "cyan", "portable_sql": "blue", "data": "magenta", "snapshot": "dim"}


def kind_of(name: str) -> str:
    """The kind of a step by its file name or step name (sample data only)."""
    if name == "ir.json":
        return "snapshot"
    if name.endswith(".py") or "backfill" in name:
        return "data"
    if name.endswith((".up.sql", ".down.sql")):
        return "portable_sql"
    return "ddl"


def file_text(o: Out, name: str, *, pad: int = 0) -> Text:
    """A file or step name coloured by what it does: the stem by kind, the
    suffix dim."""
    stem, dot, suffix = name.partition(".")
    t = Text()
    t.append(stem, style=KIND_STYLE[kind_of(name)])
    if dot:
        t.append(dot + suffix, style="dim")
    if pad > len(name):
        t.append(" " * (pad - len(name)))
    return t


LONG_LIST = 12
SHOWN_FIRST = 6


class Out:
    """The two streams, with the TTY/plain switch every variant reads."""

    def __init__(self, *, plain: bool, verbose: bool, record: bool, width: int):
        kwargs = dict(width=width, highlight=False, soft_wrap=False, markup=False)
        if plain:
            kwargs.update(no_color=True, force_terminal=False, force_jupyter=False)
        else:
            kwargs.update(force_terminal=True)
        if record:
            import io

            self.out = Console(record=True, file=io.StringIO(), **kwargs)
            self.err = self.out
        else:
            self.out = Console(**kwargs)
            self.err = Console(stderr=True, **kwargs)
        self.plain = plain
        self.verbose = verbose

    def glyph(self, key: str) -> str:
        return GLYPH[key][1 if self.plain else 0]

    def state(self, word: str) -> Text:
        return Text(word, style=STATE_STYLE.get(word, ""))

    def names(self, names: list[str]) -> tuple[list[str], int]:
        """The names to show, and how many are hidden (0 with --verbose)."""
        if self.verbose or len(names) <= LONG_LIST:
            return names, 0
        return names[:SHOWN_FIRST], len(names) - SHOWN_FIRST


def prompt(o: Out, argv: str) -> None:
    o.out.print(Text(f"$ {argv}", style="bold" if not o.plain else ""))


def where(o: Out, *, connected: bool = True, url: str | None = None) -> None:
    """The first line of every verb: which database, and how it was reached."""
    t = Text()
    t.append(DB["name"], style="bold")
    t.append(" · ")
    if connected:
        t.append(redact(url or DB["url"]), style="dim")
    else:
        t.append(DB["directory"], style="dim")
    o.out.print(t)


def count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def role_of(f: File) -> str:
    if f.role == "snapshot":
        return "snapshot"
    if f.role in ("backfill", "data step"):
        return f.role
    return f"{f.role}, {f.direction} ({f.dialect})"


# -- variant 0: today ----------------------------------------------------------


def today_init(o: Out) -> None:
    o.out.print(INIT["block"], end="")
    o.out.print(f"Wrote {INIT['written']} and created {INIT['created']}")


def today_new(o: Out, m: NewMigration) -> None:
    o.out.print(m.directory)
    for f in m.files:
        o.out.print(f"  {f.name}")
    summary = "; ".join(f"{k}: {', '.join(v)}" for k, v in m.changes.items())
    o.out.print("\n".join([summary, *m.notes]))
    for w in m.warnings:
        o.err.print(f"warning: {w}")


def today_check(o: Out) -> None:
    prompt(o, CHECK_OK["argv"])
    o.out.print(f"ok: models match {CHECK_OK['head']}")
    prompt(o, CHECK_BAD["argv"])
    for kind, message in CHECK_BAD["problems"]:
        o.err.print(f"{kind}: {message}")


def today_up(o: Out) -> None:
    w = max(len(s[0]) for s in UP_RUN["steps"])
    sw = max(len(s[1]) for s in UP_RUN["steps"])
    for mig, step, state, ms, error in UP_RUN["steps"]:
        if state == "applied":
            o.out.print(f"{mig:<{w}}  {step:<{sw}}  applied ({ms} ms)")
        else:
            o.out.print(f"{mig:<{w}}  {step:<{sw}}  failed ({ms} ms)")
    o.err.print(f"{UP_RUN['steps'][-1][4]}\n{UP_RUN['hint']}")


def today_down(o: Out) -> None:
    w = max(len(p[0]) for p in DOWN_RUN["plan"])
    o.out.print("down reverts, in this order:")
    for mig, step, nothing in DOWN_RUN["plan"]:
        line = f"  {mig:<{w}}  {step}"
        if nothing:
            line += f"  (nothing to reverse: {nothing})"
        o.out.print(line)
    o.out.print(f"{DOWN_RUN['question']} [y/N]: y")
    sw = max(len(r[1]) for r in DOWN_RUN["results"])
    for mig, step, state, ms in DOWN_RUN["results"]:
        text = f"reverted ({ms} ms)" if state == "reverted" else f"nothing to reverse: a data step with no down"
        o.out.print(f"{mig:<{w}}  {step:<{sw}}  {text}")


def today_status(o: Out) -> None:
    s = STATUS
    lines = [f"{s['database']} ({s['dialect']}) · {s['table']}", ""]
    names = [m.name for m in s["migrations"]] + s["ahead"]
    nw = max(len(n) for n in names)
    expanded = [m for m in s["migrations"] if m.expands]
    fw = max((len(st.file) for m in expanded for st in m.steps), default=0)
    for m in s["migrations"]:
        lines.append(f"{m.name:<{nw}}  {m.state}")
        if m not in expanded:
            continue
        for st in m.steps:
            flags = "".join(f"  [{f}]" for f in st.flags)
            shown = st.state if st.rows_done is None else f"{st.state} at {st.rows_done:,} rows"
            lines.append(f"  {st.file:<{fw}}  {shown}{flags}")
            if st.applied_checksum:
                lines.append(f"    applied   sha384:{st.applied_checksum}")
                lines.append(f"    on disk   sha384:{st.on_disk_checksum}")
            elif st.error and st.state == "failed":
                lines.extend(f"    {ln}" for ln in st.error.splitlines())
    for n in s["ahead"]:
        lines.append(f"{n:<{nw}}  applied, not in the directory")
    lines += ["", s["refusal"]]
    o.out.print("\n".join(lines))


def today_drift(o: Out) -> None:
    for w in DRIFT["warnings"]:
        o.err.print(f"warning: {w}")
    o.out.print(f"drift against {DRIFT['against']}:")
    for _, _, line in DRIFT["lines"]:
        o.out.print(f"  {line}")


def today_baseline(o: Out) -> None:
    b = BASELINE
    counted = f"{count(b['steps'], 'step')}; {count(len(b['data_steps']), 'data step')} listed, not run"
    o.out.print(f"recorded {b['recorded'][0]} … {b['recorded'][-1]} as baseline ({counted})")
    for d in b["data_steps"]:
        o.out.print(f"  {d}  recorded, not run")
    o.out.print(b["not_compared"])


def today_rerecord(o: Out) -> None:
    r = RERECORD
    o.out.print(
        f"re-recorded {r['file']} (sha384:{r['old']} → sha384:{r['new']}); "
        "the next up continues from its cursor; nothing was run"
    )


# -- variant A: headed ---------------------------------------------------------


def heading(o: Out, key: str, title: str, where: str = "", *, err: bool = False) -> None:
    style = {"ok": "bold green", "warn": "bold yellow", "err": "bold red", "pending": "bold yellow", "partial": "bold yellow"}[key]
    t = Text()
    t.append(o.glyph(key), style=style)
    t.append(" ")
    if isinstance(title, Text):
        t.append_text(title)
    else:
        t.append(title, style="bold")
    if where:
        t.append(f"  {where}", style="dim")
    (o.err if err else o.out).print(t)


def name_block(o: Out, label: str, names: list[str]) -> None:
    shown, hidden = o.names(names)
    o.out.print(Text(f"  {count(len(names), label)}", style="bold"))
    cols = Columns([Text(n) for n in shown], padding=(0, 2), equal=False, column_first=True)
    o.out.print(_indent(cols, 4))
    if hidden:
        o.out.print(Text(f"    … and {hidden} more (--verbose shows them all)", style="dim"))


def _indent(renderable, n: int):
    from rich.padding import Padding

    return Padding(renderable, (0, 0, 0, n), expand=False)


def a_init(o: Out) -> None:
    o.out.print(Text(INIT["block"].rstrip("\n"), style="dim"))
    heading(o, "ok", f"Wrote {INIT['written']}", f"created {INIT['created']}")


def a_new(o: Out, m: NewMigration) -> None:
    where(o, connected=False)
    heading(o, "ok", f"Wrote {m.directory}")
    fw = max(len(f.name) for f in m.files)
    for f in m.files:
        t = Text("  ")
        t.append_text(file_text(o, f.name, pad=fw))
        t.append("  ")
        t.append(role_of(f), style="dim")
        o.out.print(t)
    o.out.print()
    for label, names in m.changes.items():
        noun = label.removeprefix("new ").removeprefix("changed ").removeprefix("dropped ")
        singular = noun[:-1] if noun.endswith("s") else noun
        adj = label.split()[0]
        name_block(o, f"{adj} {singular}", names)
    for note in m.notes:
        t = Text("  ")
        t.append(o.glyph("bullet"), style="dim")
        t.append(f" {note}")
        o.out.print(t)
    for w in m.warnings:
        heading(o, "warn", "warning", w) if o.plain else _warn_line(o, w)


def _warn_line(o: Out, w: str) -> None:
    t = Text()
    t.append(o.glyph("warn"), style="bold yellow")
    t.append(" warning  ", style="bold yellow")
    t.append(w)
    o.err.print(t)


def a_check(o: Out) -> None:
    prompt(o, CHECK_OK["argv"])
    where(o, connected=False)
    if o.plain:
        o.out.print(f"ok: models match {CHECK_OK['head']}")
    else:
        heading(o, "ok", "models match", CHECK_OK["head"])
    prompt(o, CHECK_BAD["argv"])
    where(o, connected=False)
    heading(o, "err", f"{count(len(CHECK_BAD['problems']), 'problem')} since {CHECK_OK['head']}", err=True)
    for kind, message in CHECK_BAD["problems"]:
        t = Text("  ")
        t.append(f"{kind:<12}", style="bold red")
        t.append(message)
        o.err.print(t)


def a_up(o: Out) -> None:
    where(o)
    applied = sum(st[2] == "applied" for st in UP_RUN["steps"])
    failed = sum(st[2] == "failed" for st in UP_RUN["steps"])
    heading(o, "partial", "up stopped", f"{count(applied, 'step')} applied · {failed} failed")
    sw = max(len(s[1]) for s in UP_RUN["steps"])
    for mig, step, state, ms, error in UP_RUN["steps"]:
        g = "ok" if state == "applied" else "err"
        t = Text("  ")
        t.append(o.glyph(g), style="green" if g == "ok" else "bold red")
        t.append(f" {mig}  ")
        t.append_text(file_text(o, step, pad=sw))
        t.append("  ")
        t.append(state, style=STATE_STYLE[state])
        t.append(f"  {ms / 1000:.1f}s", style="dim")
        o.out.print(t)
    o.out.print()
    heading(o, "err", "03_backfill_transaction failed", f"after {UP_RUN['rows_done']:,} rows", err=True)
    o.err.print(_indent(Text(UP_RUN["steps"][-1][4], style="red"), 2))
    o.err.print(_indent(Text(UP_RUN["hint"]), 2))


def a_down(o: Out) -> None:
    where(o)
    heading(o, "warn", "down reverts, newest first", "0002_add_team, 4 steps")
    sw = max(len(p[1]) for p in DOWN_RUN["plan"])
    for mig, step, nothing in DOWN_RUN["plan"]:
        t = Text(f"  {mig}  ")
        t.append_text(file_text(o, step, pad=sw))
        if nothing:
            t.append(f"  nothing to reverse: {nothing}", style="dim")
        o.out.print(t)
    o.out.print(Text(f"{DOWN_RUN['question']} [y/N]: y", style="bold"))
    for mig, step, state, ms in DOWN_RUN["results"]:
        t = Text("  ")
        t.append(o.glyph("ok" if state == "reverted" else "bullet"), style="green" if state == "reverted" else "dim")
        t.append(f" {mig}  ")
        t.append_text(file_text(o, step, pad=sw))
        t.append("  ")
        t.append(state, style=STATE_STYLE[state])
        if ms:
            t.append(f"  {ms / 1000:.1f}s", style="dim")
        o.out.print(t)
    heading(o, "ok", "reverted 0002_add_team", "4 steps, now at 0001_initial")


def a_status(o: Out) -> None:
    where(o)
    s = STATUS
    heading(o, "warn", "1 failed · 2 pending · 1 ahead", s["table"])
    nw = max(len(m.name) for m in s["migrations"] + [Migration(a, "", []) for a in s["ahead"]])
    for m in s["migrations"]:
        key = {"pending": "pending", "applied": "ok", "applied (baseline)": "ok"}.get(m.state, "err" if "checksum" in m.state else "partial")
        t = Text("  ")
        t.append(o.glyph(key), style={"ok": "green", "pending": "yellow", "partial": "yellow", "err": "red"}[key])
        t.append(f" {m.name:<{nw}}  ")
        t.append(m.state, style=STATE_STYLE.get(m.state, "yellow"))
        if m.applied_at:
            t.append(f"  {m.applied_at}", style="dim")
        o.out.print(t)
        if not m.expands:
            continue
        fw = max(len(st.file) for st in m.steps)
        for st in m.steps:
            t = Text("      ")
            t.append_text(file_text(o, st.file, pad=fw))
            t.append("  ")
            shown = st.state if st.rows_done is None else f"{st.state} at {st.rows_done:,} rows"
            t.append(shown, style=STATE_STYLE.get(st.state, ""))
            for f in st.flags:
                t.append(f"  [{f}]", style="bold yellow")
            o.out.print(t)
            if st.applied_checksum:
                o.out.print(Text(f"        applied  sha384:{st.applied_checksum}", style="dim"))
                o.out.print(Text(f"        on disk  sha384:{st.on_disk_checksum}", style="dim"))
            elif st.error:
                o.out.print(_indent(Text(st.error, style="red"), 8))
    for a in s["ahead"]:
        t = Text("  ")
        t.append(o.glyph("err"), style="red")
        t.append(f" {a:<{nw}}  ")
        t.append("applied, not in the directory", style="red")
        o.out.print(t)
    o.out.print()
    heading(o, "err", "needs attention", "", err=True)
    o.err.print(_indent(Text(s["refusal"]), 2))


def a_drift(o: Out) -> None:
    where(o)
    for w in DRIFT["warnings"]:
        _warn_line(o, w)
    heading(o, "err", f"{count(len(DRIFT['lines']), 'difference')} against {DRIFT['against']}")
    for _, _, line in DRIFT["lines"]:
        t = Text("  ")
        t.append(o.glyph("bullet"), style="red")
        t.append(f" {line}")
        o.out.print(t)
    o.out.print()
    prompt(o, "ferro migrate drift   # a clean database")
    where(o, url=CLEAN_DB_URL)
    if o.plain:
        o.out.print(f"no drift against {DRIFT['against']}")
    else:
        heading(o, "ok", "no drift", f"against {DRIFT['against']}")


def a_baseline(o: Out) -> None:
    where(o)
    b = BASELINE
    heading(o, "ok", f"recorded {b['recorded'][0]} … {b['recorded'][-1]} as baseline", f"{count(b['steps'], 'step')}")
    for d in b["data_steps"]:
        mig, _, file = d.rpartition("/")
        t = Text(f"  {mig}/")
        t.append_text(file_text(o, file))
        t.append("  ")
        t.append("recorded, not run", style="dim")
        o.out.print(t)
    o.out.print(_indent(Text(b["not_compared"], style="dim"), 2))


def a_rerecord(o: Out) -> None:
    where(o)
    r = RERECORD
    mig, _, file = r["file"].rpartition("/")
    title = Text("re-recorded ", style="bold")
    title.append(f"{mig}/", style="bold")
    title.append_text(file_text(o, file))
    heading(o, "ok", title, "nothing was run")
    t = Text(f"  sha384:{r['old']} ")
    t.append(o.glyph("arrow"), style="dim")
    t.append(f" sha384:{r['new']}")
    o.out.print(t)
    o.out.print(Text("  the next up continues from its cursor", style="dim"))


# -- variant B: tabular --------------------------------------------------------


def table(o: Out, *cols: str, title: str | None = None, wide_last: bool = False) -> Table:
    t = Table(box=box.SIMPLE_HEAD if not o.plain else box.ASCII2, show_edge=False, pad_edge=False, title=title, title_justify="left", title_style="bold", header_style="bold dim" if not o.plain else "", expand=wide_last)
    for i, c in enumerate(cols):
        last = i == len(cols) - 1
        t.add_column(c, overflow="fold" if last else "ellipsis", no_wrap=not last, ratio=1 if (last and wide_last) else None)
    return t


def b_init(o: Out) -> None:
    o.out.print(Text(INIT["block"].rstrip("\n"), style="dim"))
    t = table(o, "Wrote", "Created")
    t.add_row(INIT["written"], INIT["created"])
    o.out.print(t)


def b_new(o: Out, m: NewMigration) -> None:
    where(o, connected=False)
    t = table(o, "File", "Kind", "Direction", "Dialect", title=m.directory)
    for f in m.files:
        t.add_row(f.name, f.role, f.direction or "", f.dialect or "")
    o.out.print(t)
    c = table(o, "Change", "Count", "Names")
    for label, names in m.changes.items():
        shown, hidden = o.names(names)
        text = ", ".join(shown) + (f", … {hidden} more" if hidden else "")
        c.add_row(label, str(len(names)), text)
    o.out.print(c)
    if m.notes:
        n = table(o, "File", "To do")
        for note in m.notes:
            file, _, rest = note.partition(": ")
            n.add_row(file, rest)
        o.out.print(n)
    for w in m.warnings:
        o.err.print(Text(f"warning: {w}", style="yellow"))


def b_check(o: Out) -> None:
    prompt(o, CHECK_OK["argv"])
    where(o, connected=False)
    o.out.print(Text(f"ok: models match {CHECK_OK['head']}", style="green"))
    prompt(o, CHECK_BAD["argv"])
    where(o, connected=False)
    t = table(o, "Problem", "Detail", title=f"{count(len(CHECK_BAD['problems']), 'problem')} since {CHECK_OK['head']}")
    for kind, message in CHECK_BAD["problems"]:
        t.add_row(Text(kind, style="bold red"), message)
    o.err.print(t)


def b_up(o: Out) -> None:
    where(o)
    t = table(o, "Migration", "Step", "Result", "Time", title="ferro migrate up")
    for mig, step, state, ms, _ in UP_RUN["steps"]:
        t.add_row(mig, step, o.state(state), f"{ms / 1000:.1f}s")
    o.out.print(t)
    e = table(o, "Failed", "Why", "Next", wide_last=True)
    e.add_row(Text("03_backfill_transaction", style="bold red"), UP_RUN["steps"][-1][4], UP_RUN["hint"])
    o.err.print(e)


def b_down(o: Out) -> None:
    where(o)
    t = table(o, "#", "Migration", "Step", "Note", title="down reverts, in this order")
    for i, (mig, step, nothing) in enumerate(DOWN_RUN["plan"], 1):
        t.add_row(str(i), mig, step, Text(f"nothing to reverse: {nothing}", style="dim") if nothing else "")
    o.out.print(t)
    o.out.print(Text(f"{DOWN_RUN['question']} [y/N]: y", style="bold"))
    r = table(o, "Migration", "Step", "Result", "Time")
    for mig, step, state, ms in DOWN_RUN["results"]:
        r.add_row(mig, step, o.state(state), f"{ms / 1000:.1f}s" if ms else "")
    o.out.print(r)


def b_status(o: Out) -> None:
    where(o)
    s = STATUS
    t = table(o, "Migration", "State", "Applied at", "Steps", title=s["table"])
    for m in s["migrations"]:
        done = sum(st.state.startswith("applied") for st in m.steps)
        t.add_row(m.name, o.state(m.state) if m.state in STATE_STYLE else Text(m.state, style="yellow"), m.applied_at or "", f"{done}/{len(m.steps)}")
    for a in s["ahead"]:
        t.add_row(a, Text("applied, not in the directory", style="red"), "", "")
    o.out.print(t)
    d = table(o, "Migration", "Step", "State", "Class", title="needs attention")
    details = []
    for m in s["migrations"]:
        if not m.expands:
            continue
        for st in m.steps:
            if st.state in ("applied", "pending") and not st.flags:
                continue
            shown = st.state if st.rows_done is None else f"{st.state} at {st.rows_done:,} rows"
            d.add_row(m.name, st.file, o.state(st.state) if st.state in STATE_STYLE else Text(shown, style="red"), st.step_class)
            if st.error:
                details.append((st.file, st.error))
            elif st.applied_checksum:
                details.append((st.file, f"applied sha384:{st.applied_checksum}, on disk sha384:{st.on_disk_checksum}"))
    o.out.print(d)
    for file, text in details:
        o.out.print(Text(f"  {file}", style="bold"))
        o.out.print(_indent(Text(text, style="red" if "Error" in text else "dim"), 4))
    o.err.print(Text(s["refusal"], style="bold red"))


def b_drift(o: Out) -> None:
    where(o)
    for w in DRIFT["warnings"]:
        o.err.print(Text(f"warning: {w}", style="yellow"))
    t = table(o, "Object", "Kind", "Finding", title=f"drift against {DRIFT['against']}")
    for obj, kind, line in DRIFT["lines"]:
        t.add_row(obj, kind, line)
    o.out.print(t)
    o.out.print()
    prompt(o, "ferro migrate drift   # a clean database")
    where(o, url=CLEAN_DB_URL)
    o.out.print(Text(f"no drift against {DRIFT['against']}", style="green"))


def b_baseline(o: Out) -> None:
    where(o)
    b = BASELINE
    t = table(o, "Recorded", "Steps", "Listed, not run", title="baseline")
    t.add_row(f"{b['recorded'][0]} … {b['recorded'][-1]}", str(b["steps"]), "\n".join(b["data_steps"]))
    o.out.print(t)
    o.out.print(Text(b["not_compared"], style="dim"))


def b_rerecord(o: Out) -> None:
    where(o)
    r = RERECORD
    t = table(o, "Re-recorded", "Was", "Now", "Next up")
    t.add_row(r["file"], f"sha384:{r['old']}", f"sha384:{r['new']}", "continues from its cursor")
    o.out.print(t)


# -- variant C: gutter ---------------------------------------------------------

GUTTER = 11


def gutter(o: Out, verb: str, text: str, style: str = "bold green", *, err: bool = False) -> None:
    con = o.err if err else o.out
    lines = []
    for para in text.splitlines() or [""]:
        lines += textwrap.wrap(para, width=con.width - GUTTER - 1, break_long_words=False, break_on_hyphens=False) or [""]
    t = Text()
    for i, line in enumerate(lines):
        if i:
            t.append("\n")
        t.append(f"{verb if i == 0 else '':>{GUTTER}}", style=style)
        t.append(f" {line}")
    con.print(t)


def c_init(o: Out) -> None:
    for ln in INIT["block"].rstrip("\n").splitlines():
        gutter(o, "", ln, "dim")
    gutter(o, "Wrote", INIT["written"])
    gutter(o, "Created", INIT["created"])


def c_new(o: Out, m: NewMigration) -> None:
    where(o, connected=False)
    gutter(o, "Wrote", m.directory)
    for f in m.files:
        verb = {"snapshot": "Snapshot", "backfill": "Backfill", "data step": "Data step"}.get(f.role, f.direction.capitalize())
        gutter(o, verb, f"{f.name}" + (f"  ({f.dialect})" if f.dialect else ""), "cyan")
    for label, names in m.changes.items():
        adj, _, noun = label.partition(" ")
        verb = {"new": "Added", "changed": "Changed", "dropped": "Dropped", "renamed": "Renamed", "removed": "Removed"}[adj]
        shown, hidden = o.names(names)
        text = f"{count(len(names), noun.rstrip('s'))}: " + ", ".join(shown) + (f", and {hidden} more (--verbose)" if hidden else "")
        gutter(o, verb, text)
    for note in m.notes:
        gutter(o, "To do", note, "bold yellow")
    for w in m.warnings:
        gutter(o, "Warning", w, "bold yellow", err=True)


def c_check(o: Out) -> None:
    prompt(o, CHECK_OK["argv"])
    where(o, connected=False)
    if o.plain:
        o.out.print(f"ok: models match {CHECK_OK['head']}")
    else:
        gutter(o, "Checked", f"models match {CHECK_OK['head']}")
    prompt(o, CHECK_BAD["argv"])
    where(o, connected=False)
    for kind, message in CHECK_BAD["problems"]:
        gutter(o, kind.capitalize(), message, "bold red", err=True)


def c_up(o: Out) -> None:
    gutter(o, "Database", f"{DB['name']} · {redact(DB['url'])}", "bold cyan")
    gutter(o, "Migrating", "0002_add_team, 3 steps", "bold cyan")
    for mig, step, state, ms, _ in UP_RUN["steps"]:
        verb = "Applied" if state == "applied" else "Failed"
        gutter(o, verb, f"{mig}  {step}  ({ms / 1000:.1f}s)", "bold green" if state == "applied" else "bold red")
    gutter(o, "Error", UP_RUN["steps"][-1][4].splitlines()[0], "bold red", err=True)
    gutter(o, "", UP_RUN["steps"][-1][4].splitlines()[1], "", err=True)
    gutter(o, "Next", UP_RUN["hint"], "bold yellow", err=True)


def c_down(o: Out) -> None:
    where(o)
    gutter(o, "Reverting", "0002_add_team, 4 steps, newest first", "bold cyan")
    for mig, step, nothing in DOWN_RUN["plan"]:
        gutter(o, "", f"{mig}  {step}" + (f"  (nothing to reverse: {nothing})" if nothing else ""), "")
    o.out.print(Text(f"{DOWN_RUN['question']} [y/N]: y", style="bold"))
    for mig, step, state, ms in DOWN_RUN["results"]:
        if state == "reverted":
            gutter(o, "Reverted", f"{mig}  {step}  ({ms / 1000:.1f}s)")
        else:
            gutter(o, "Skipped", f"{mig}  {step}  nothing to reverse", "dim")
    gutter(o, "Now at", "0001_initial", "bold cyan")


def c_status(o: Out) -> None:
    s = STATUS
    gutter(o, "Database", f"{DB['name']} · {redact(DB['url'])}", "bold cyan")
    gutter(o, "Table", s["table"], "bold cyan")
    verbs = {"applied": ("Applied", "bold green"), "applied (baseline)": ("Baseline", "bold green"), "pending": ("Pending", "bold yellow"), "applied (different checksum)": ("Edited", "bold red")}
    for m in s["migrations"]:
        verb, style = verbs.get(m.state, ("Partial", "bold yellow"))
        gutter(o, verb, m.name + (f"  {m.applied_at}" if m.applied_at else "") + (f"  ({m.state})" if verb in ("Partial", "Edited") else ""), style)
        if not m.expands:
            continue
        for st in m.steps:
            shown = st.state if st.rows_done is None else f"{st.state} at {st.rows_done:,} rows"
            flags = "".join(f"  [{f}]" for f in st.flags)
            gutter(o, "", f"{st.file}  {shown}{flags}", "")
            if st.applied_checksum:
                gutter(o, "", f"  applied sha384:{st.applied_checksum}, on disk sha384:{st.on_disk_checksum}", "dim")
            elif st.error:
                gutter(o, "", f"  {st.error}", "red")
    for a in s["ahead"]:
        gutter(o, "Ahead", f"{a}  applied, not in the directory", "bold red")
    gutter(o, "Attention", s["refusal"], "bold red", err=True)


def c_drift(o: Out) -> None:
    where(o)
    for w in DRIFT["warnings"]:
        gutter(o, "Warning", w, "bold yellow", err=True)
    gutter(o, "Drift", f"against {DRIFT['against']}, {count(len(DRIFT['lines']), 'difference')}", "bold red")
    for obj, kind, line in DRIFT["lines"]:
        verb = "Missing" if "is missing" in line else "Extra" if "is extra" in line else "Differs"
        gutter(o, verb, line, "red")
    o.out.print()
    prompt(o, "ferro migrate drift   # a clean database")
    where(o, url=CLEAN_DB_URL)
    if o.plain:
        o.out.print(f"no drift against {DRIFT['against']}")
    else:
        gutter(o, "No drift", f"against {DRIFT['against']}")


def c_baseline(o: Out) -> None:
    where(o)
    b = BASELINE
    gutter(o, "Recorded", f"{b['recorded'][0]} … {b['recorded'][-1]} as baseline ({count(b['steps'], 'step')})")
    for d in b["data_steps"]:
        gutter(o, "Listed", f"{d}  recorded, not run", "dim")
    gutter(o, "Note", b["not_compared"], "dim")


def c_rerecord(o: Out) -> None:
    where(o)
    r = RERECORD
    gutter(o, "Re-recorded", f"{r['file']}  sha384:{r['old']} {o.glyph('arrow')} sha384:{r['new']}")
    gutter(o, "Next up", "continues from its cursor; nothing was run", "dim")


# -- wiring --------------------------------------------------------------------

VARIANTS: dict[str, tuple[str, dict[str, object]]] = {
    "0": ("Today", {"init": today_init, "new": today_new, "check": today_check, "up": today_up, "down": today_down, "status": today_status, "drift": today_drift, "baseline": today_baseline, "rerecord": today_rerecord}),
    "A": ("Headed", {"init": a_init, "new": a_new, "check": a_check, "up": a_up, "down": a_down, "status": a_status, "drift": a_drift, "baseline": a_baseline, "rerecord": a_rerecord}),
    "B": ("Tabular", {"init": b_init, "new": b_new, "check": b_check, "up": b_up, "down": b_down, "status": b_status, "drift": b_drift, "baseline": b_baseline, "rerecord": b_rerecord}),
    "C": ("Gutter", {"init": c_init, "new": c_new, "check": c_check, "up": c_up, "down": c_down, "status": c_status, "drift": c_drift, "baseline": c_baseline, "rerecord": c_rerecord}),
}


def render(o: Out, variant: str, verbs: list[str]) -> None:
    _, fns = VARIANTS[variant]
    for verb in verbs:
        fn = fns[verb]
        if verb == "new":
            for m in (NEW_INITIAL, NEW_TEAM):
                prompt(o, m.argv)
                fn(o, m)
                o.out.print()
            continue
        if verb not in ("check",):
            argv = {"init": INIT, "up": UP_RUN, "down": DOWN_RUN, "status": STATUS, "drift": DRIFT, "baseline": BASELINE, "rerecord": RERECORD}[verb]["argv"]
            prompt(o, argv)
        fn(o)
        o.out.print()


def switcher(o: Out, variant: str) -> None:
    keys = list(VARIANTS)
    i = keys.index(variant)
    prev, nxt = keys[i - 1], keys[(i + 1) % len(keys)]
    t = Text()
    t.append("  ◀ " if not o.plain else "  < ", style="dim")
    t.append(prev, style="dim")
    t.append(f"   {variant} · {VARIANTS[variant][0]}   ", style="reverse bold")
    t.append(nxt, style="dim")
    t.append(" ▶   " if not o.plain else " >   ", style="dim")
    t.append(f"uv run prototype/migrate_output.py --variant {nxt}", style="dim")
    o.out.print(t)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--variant", choices=list(VARIANTS), default="A")
    p.add_argument("--verb", choices=VERBS, action="append")
    p.add_argument("--plain", action="store_true", help="render as a pipe sees it")
    p.add_argument("--verbose", action="store_true", help="long lists in full")
    p.add_argument("--width", type=int, default=100)
    p.add_argument("--html", help="write every variant, TTY and plain, to one HTML file")
    args = p.parse_args()
    verbs = args.verb or VERBS

    if args.html:
        panes = []
        for key, (name, _) in VARIANTS.items():
            for plain in (False, True):
                for verb in verbs:
                    o = Out(plain=plain, verbose=args.verbose, record=True, width=args.width)
                    render(o, key, [verb])
                    html = o.out.export_html(inline_styles=True, theme=TERMINAL, code_format="{code}")
                    panes.append((key, name, plain, verb, html.rstrip()))
        from pathlib import Path

        Path(args.html).write_text(_page(panes))
        print(f"wrote {args.html}")
        return 0

    o = Out(plain=args.plain, verbose=args.verbose, record=False, width=args.width)
    render(o, args.variant, verbs)
    switcher(o, args.variant)
    return 0


BLURBS = {
    "0": "What the CLI prints today. Every list is one comma-joined line; files carry no role; a refusal looks like any other line.",
    "A": "A glyph and a bold heading per result, details indented beneath it. Name lists wrap in columns with a count, long ones collapse to the first few. Errors get a red heading.",
    "B": "Every result is a table with a header rule: files, changes, migrations, findings, steps. Long prose sits under its table, never inside a cell.",
    "C": "A right-aligned coloured verb in a fixed gutter, one sentence per line, lists wrapped with a hanging indent. No tables, no boxes. The cargo and uv shape.",
}


def _page(panes) -> str:
    """The shareable page: every variant, TTY and plain, per verb."""
    import html as h

    blocks = "\n".join(
        f'<pre class="term" data-variant="{k}" data-plain="{int(plain)}" data-verb="{verb}" hidden>{html}</pre>'
        for k, name, plain, verb, html in panes
    )
    verb_nav = "".join(f'<a href="#" data-jump="{v}">{v}</a>' for v in VERBS)
    verb_sections = "\n".join(
        f'<section class="verb" id="verb-{v}"><h2><span class="prompt">ferro migrate</span> {v}</h2>'
        f'<div class="pane" data-pane="{v}"></div></section>'
        for v in VERBS
    )
    tabs = "".join(
        f'<button type="button" class="tab" data-tab="{k}" id="tab-{k}"><b>{k}</b> {name}</button>'
        for k, (name, _) in VARIANTS.items()
    )
    blurbs = "".join(f'<p class="blurb" data-blurb="{k}" hidden>{h.escape(b)}</p>' for k, b in BLURBS.items())
    return f"""<title>ferro migrate Output Prototype</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;700&display=swap">
<style>
/* Layout: a sticky switcher bar, then one terminal pane per verb; the pane is a fixed dark terminal, the chrome follows the theme. */
:root {{
  --bg: #f6f4ef; --fg: #1d1b17; --muted: #6b665c; --line: #d9d4c8; --accent: #b4452a; --accent-ink: #fff;
  --term-bg: #11131a; --term-fg: #dee2e8; --term-line: #2a2e3a;
  --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg: #1a1c22; --fg: #e8e6e0; --muted: #9a968c; --line: #30333d; --accent: #e0714f; --accent-ink: #1a1c22; color-scheme: dark }} }}
:root[data-theme="dark"] {{ --bg: #1a1c22; --fg: #e8e6e0; --muted: #9a968c; --line: #30333d; --accent: #e0714f; --accent-ink: #1a1c22; color-scheme: dark }}
body {{ background: var(--bg); color: var(--fg); font-family: var(--sans); font-size: 15px; line-height: 1.5; margin: 0; }}
.bar {{ position: sticky; top: env(safe-area-inset-top, 0px); z-index: 2; background: var(--bg); border-bottom: 1px solid var(--line);
  padding-block: 10px; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: flex; flex-wrap: wrap; gap: 10px 18px; align-items: center; }}
.bar h1 {{ font-size: 15px; font-weight: 600; margin: 0; letter-spacing: .01em; }}
.bar h1 small {{ color: var(--muted); font-weight: 400; margin-left: 8px; }}
.tabs {{ display: flex; gap: 4px; border: 1px solid var(--line); border-radius: 8px; padding: 3px; }}
.tab {{ font: inherit; font-size: 13px; color: var(--fg); background: none; border: 0; border-radius: 6px; padding: 4px 10px; cursor: pointer; }}
.tab b {{ font-family: var(--mono); font-weight: 700; margin-right: 4px; }}
.tab[aria-selected="true"] {{ background: var(--accent); color: var(--accent-ink); }}
.tab:focus-visible, .toggle button:focus-visible, .jump a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
.toggle {{ display: flex; gap: 0; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
.toggle button {{ font: inherit; font-size: 13px; color: var(--fg); background: none; border: 0; padding: 5px 10px; cursor: pointer; }}
.toggle button[aria-pressed="true"] {{ background: var(--fg); color: var(--bg); }}
.hint {{ color: var(--muted); font-size: 13px; }}
.hint kbd {{ font-family: var(--mono); font-size: 12px; border: 1px solid var(--line); border-radius: 4px; padding: 0 5px; }}
main {{ max-width: 1040px; margin: 0 auto; padding-block: 18px 48px; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); display: flex; flex-direction: column; gap: 26px; }}
.blurb {{ margin: 0; max-width: 68ch; color: var(--muted); }}
.jump {{ display: flex; flex-wrap: wrap; gap: 6px 14px; font-family: var(--mono); font-size: 13px; }}
.jump a {{ color: var(--accent); text-decoration: none; }}
.jump a:hover {{ text-decoration: underline; }}
.verb {{ min-width: 0; }}
.verb h2 {{ font-family: var(--mono); font-size: 14px; font-weight: 700; margin: 0 0 8px; text-wrap: balance; }}
.verb h2 .prompt {{ color: var(--muted); font-weight: 400; }}
.pane {{ background: var(--term-bg); color: var(--term-fg); border: 1px solid var(--term-line); border-radius: 8px; overflow-x: auto; }}
.term {{ margin: 0; padding: 14px 16px; font-family: var(--mono); font-size: 12.5px; line-height: 1.45; font-variant-numeric: tabular-nums; white-space: pre; }}
@media (max-width: 600px) {{ .bar h1 small {{ display: none; }} .term {{ font-size: 11.5px; }} }}
@media (prefers-reduced-motion: no-preference) {{ .tab, .toggle button {{ transition: background .12s; }} }}
</style>
<div class="bar">
  <h1>ferro migrate output <small>issue #646, prototype</small></h1>
  <div class="tabs" role="tablist" aria-label="Variant">{tabs}</div>
  <div class="toggle" role="group" aria-label="Stream">
    <button type="button" id="mode-tty" data-mode="0">TTY</button>
    <button type="button" id="mode-plain" data-mode="1">plain / piped</button>
  </div>
  <span class="hint"><kbd>←</kbd> <kbd>→</kbd> switch variant</span>
</div>
<main>
  {blurbs}
  <nav class="jump" aria-label="Verbs">{verb_nav}</nav>
  {verb_sections}
</main>
<div id="store" hidden>{blocks}</div>
<script>
(function () {{
  var keys = {list(VARIANTS)!r};
  var state = {{ variant: "A", plain: "0" }};
  function fromHash() {{ var h = location.hash.replace("#", ""); if (keys.indexOf(h) >= 0) state.variant = h; }}
  function show() {{
    document.querySelectorAll(".tab").forEach(function (t) {{ t.setAttribute("aria-selected", String(t.dataset.tab === state.variant)); }});
    document.querySelectorAll(".toggle button").forEach(function (b) {{ b.setAttribute("aria-pressed", String(b.dataset.mode === state.plain)); }});
    document.querySelectorAll(".blurb").forEach(function (p) {{ p.hidden = p.dataset.blurb !== state.variant; }});
    document.querySelectorAll(".pane").forEach(function (pane) {{
      var sel = '.term[data-variant="' + state.variant + '"][data-plain="' + state.plain + '"][data-verb="' + pane.dataset.pane + '"]';
      var src = document.querySelector("#store " + sel);
      pane.innerHTML = "";
      if (src) {{ var c = src.cloneNode(true); c.hidden = false; pane.appendChild(c); }}
    }});
    try {{ localStorage.setItem("ferro-646-plain", state.plain); }} catch (e) {{}}
  }}
  function go(delta) {{
    var i = keys.indexOf(state.variant);
    state.variant = keys[(i + delta + keys.length) % keys.length];
    history.replaceState(null, "", "#" + state.variant);
    show();
  }}
  document.querySelectorAll(".tab").forEach(function (t) {{ t.addEventListener("click", function () {{ state.variant = t.dataset.tab; history.replaceState(null, "", "#" + state.variant); show(); }}); }});
  document.querySelectorAll(".toggle button").forEach(function (b) {{ b.addEventListener("click", function () {{ state.plain = b.dataset.mode; show(); }}); }});
  document.querySelectorAll(".jump a").forEach(function (a) {{ a.addEventListener("click", function (e) {{ e.preventDefault(); var s = document.getElementById("verb-" + a.dataset.jump); if (s) s.scrollIntoView({{ behavior: "smooth", block: "start" }}); }}); }});
  document.addEventListener("keydown", function (e) {{
    var tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || e.target.isContentEditable) return;
    if (e.key === "ArrowLeft") go(-1); else if (e.key === "ArrowRight") go(1);
  }});
  window.addEventListener("hashchange", function () {{ fromHash(); show(); }});
  try {{ var p = localStorage.getItem("ferro-646-plain"); if (p === "0" || p === "1") state.plain = p; }} catch (e) {{}}
  fromHash(); show();
}})();
</script>
"""


if __name__ == "__main__":
    sys.exit(main())
