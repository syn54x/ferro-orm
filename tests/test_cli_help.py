"""``--help`` prints every ``<placeholder>`` written in a command's help source.

Help rendered as Markdown swallows ``<migration>:<step>`` as an HTML tag, so
``ferro migrate rerecord --help`` used to read "The step whose edit to
accept: :, as 0007:01." Help is plain text; this pins it for every command.
"""

import inspect
import re
from collections.abc import Iterator

import pytest

from ferro.cli import app

PLACEHOLDER = re.compile(r"<[A-Za-z_][^<>\s]*>")


def _commands(
    root, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], object]]:
    yield path, root
    for name, sub in root._commands.items():
        if name.startswith("-"):
            continue
        yield from _commands(sub, (*path, name))


def _help_source(command) -> str:
    """Every help string written for the command: summary, docstring, parameters."""
    parts = []
    if command.help:
        parts.append(command.help)
    func = command.default_command
    if func is not None:
        parts.append(inspect.getdoc(func) or "")
        for argument in command.assemble_argument_collection():
            parts.append(argument.parameter.help or "")
    return "\n".join(parts)


CASES = [
    pytest.param(path, command, id=" ".join(("ferro", *path)))
    for path, command in _commands(app._meta_parent)
]


@pytest.mark.parametrize(("path", "command"), CASES)
def test_help_keeps_every_placeholder(path, command, capsys) -> None:
    placeholders = set(PLACEHOLDER.findall(_help_source(command)))
    try:
        app([*path, "--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    # The help table wraps long lines; compare with whitespace folded away.
    folded = re.sub(r"\s+", "", out)
    missing = sorted(p for p in placeholders if re.sub(r"\s+", "", p) not in folded)
    assert not missing, f"--help dropped {missing}:\n{out}"


def test_the_scan_sees_the_placeholders_the_ticket_names() -> None:
    found = {
        " ".join(path): set(PLACEHOLDER.findall(_help_source(command)))
        for path, command in _commands(app._meta_parent)
    }
    assert {"<migration>", "<step>"} <= found["migrate rerecord"]
    assert found["migrate new"]
    assert found["migrate squash"]


MARKDOWN = re.compile(r"`|\*\*|\[[^\]]+\]\([^)]+\)")


@pytest.mark.parametrize(("path", "command"), CASES)
def test_help_carries_no_markdown(path, command) -> None:
    # Help prints as plain text, so Markdown shows up literally: a `new` in
    # backticks prints with its backticks.
    found = MARKDOWN.findall(_help_source(command))
    assert not found, f"{' '.join(('ferro', *path))} help carries Markdown {found}"


@pytest.mark.parametrize(("path", "command"), CASES)
def test_help_summary_is_one_line(path, command) -> None:
    func = command.default_command
    doc = inspect.getdoc(func) if func is not None else command.help
    summary = (doc or "").split("\n\n")[0]
    assert summary[:1].isupper() and "\n" not in summary and summary.endswith("."), (
        f"{' '.join(('ferro', *path))}: summary {summary!r} is not one capitalised line ending "
        "with a period"
    )


@pytest.mark.parametrize(
    ("flag", "parameter", "value"),
    [
        ("--steps", "steps", True),
        ("--no-steps", "steps", False),
        ("--json", "json_", True),
        ("--no-json", "json_", False),
    ],
)
def test_status_accepts_both_spellings_of_its_flags(flag, parameter, value) -> None:
    # `--no-steps` and `--no-json` shipped with `ferro migrate status`; a
    # script that passes them must keep parsing.
    _, bound, _ = app["migrate"].parse_args(
        ["status", flag], exit_on_error=False, print_error=False
    )
    assert bound.arguments[parameter] is value


def test_status_help_lists_the_negative_flags(capsys) -> None:
    try:
        app(["migrate", "status", "--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "--no-steps" in out
    assert "--no-json" in out
