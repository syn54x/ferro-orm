"""The CLI reference lists every flag each ``ferro migrate`` verb accepts.

For each verb, every ``--flag`` its ``--help`` prints, bar the global options
(``--config``, ``--database``, ``--url``, ``--help``, ``--version``), appears in
the verb's ``## `ferro migrate <verb> ...``` section of
``docs/pages/reference/cli.md``. The global options appear in its
``## Global options`` section. A flag added to the CLI and not to the page
fails here, naming the verb and the flag.
"""

from __future__ import annotations

import re

import pytest

from ferro.cli import app
from tests._docs_harness import DOCS_PAGES

CLI_REFERENCE = DOCS_PAGES / "reference" / "cli.md"
GLOBAL_OPTIONS = ("--config", "--database", "--url", "--help", "--version")
FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")


def _verbs() -> list[str]:
    migrate = app._meta_parent._commands["migrate"]
    return sorted(name for name in migrate._commands if not name.startswith("-"))


POSITIONAL = re.compile(
    r"(?<![\w-])(?P<name>[A-Z][A-Z_]*) +(?P<flag>--[a-z][a-z0-9-]*)"
)
"""A positional argument as ``--help`` lists it, with its keyword spelling:
``NAME --name``."""


def _help_flags(verb: str, capsys: pytest.CaptureFixture[str]) -> set[str]:
    """The verb's flags as ``--help`` prints them, bar the global options and
    the keyword spellings of its positional arguments (the heading names
    those, as ``ferro migrate new NAME``)."""
    try:
        app(["migrate", verb, "--help"])
    except SystemExit:
        pass
    printed = capsys.readouterr().out
    positionals = {match["flag"] for match in POSITIONAL.finditer(printed)}
    return set(FLAG.findall(printed)) - set(GLOBAL_OPTIONS) - positionals


def _section(heading: re.Pattern[str]) -> str:
    """The text of the cli.md section whose ``##`` heading matches ``heading``."""
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    sections = re.split(r"(?m)^(?=## )", text)
    found = [section for section in sections if heading.match(section)]
    assert len(found) == 1, f"cli.md has {len(found)} sections matching {heading}"
    return found[0]


@pytest.mark.parametrize("verb", _verbs())
def test_the_reference_lists_every_flag_of_the_verb(
    verb: str, capsys: pytest.CaptureFixture[str]
) -> None:
    flags = _help_flags(verb, capsys)
    section = _section(re.compile(rf"## `ferro migrate {verb}[` ]"))
    listed = set(FLAG.findall(section))
    missing = sorted(flags - listed)
    assert not missing, (
        f"ferro migrate {verb} --help prints {missing}, which its section of "
        f"docs/pages/reference/cli.md does not list"
    )


def test_the_reference_lists_every_global_option() -> None:
    section = _section(re.compile(r"## Global options"))
    missing = [flag for flag in GLOBAL_OPTIONS if flag not in section]
    assert not missing, f"cli.md's Global options section omits {missing}"


def test_every_verb_has_a_section() -> None:
    assert _verbs() == [
        "baseline",
        "check",
        "down",
        "drift",
        "init",
        "new",
        "rerecord",
        "squash",
        "status",
        "up",
    ]
