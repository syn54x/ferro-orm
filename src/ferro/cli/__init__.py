"""The ``ferro`` command.

```text
$ pip install "ferro-orm[cli]"
$ ferro migrate init
```

The ``ferro`` console script ships with the core wheel and calls
:func:`main`. The command line itself is a cyclopts app that needs the
``cli`` extra; without it :func:`main` prints the install line and exits 2.
Importing this module never imports cyclopts: :data:`app` is built on first
access, so ``import ferro`` costs nothing for the CLI.

Every verb lives on the ``migrate`` sub-app (:mod:`ferro.cli.migrate`) and
receives the three global options as one :class:`Global`, given before or
after the verb (``ferro --database app migrate up`` or
``ferro migrate up --database app``). A :class:`~ferro.exceptions.FerroError`
raised by a verb is a refusal: :func:`render_refusal` prints its message,
which names the fix, and the command exits :data:`exit_codes.REFUSED`.
A bad flag or a missing ``cli`` extra exits :data:`exit_codes.USAGE`.
"""

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from ..exceptions import FerroError
from . import exit_codes

if TYPE_CHECKING:
    from cyclopts import App

__all__ = ["Global", "app", "exit_codes", "main", "render_refusal"]

INSTALL_HINT = 'ferro\'s CLI needs the cli extra: pip install "ferro-orm[cli]"'


@dataclass(frozen=True)
class Global:
    """The global options every verb receives (ADR-0036: the whole external
    surface is these three flags and two environment variables)."""

    config: Path | None = None
    """``--config``: the config file to read, in place of the lookup."""
    database: str | None = None
    """``--database``: which configured database the verb acts on."""
    url: str | None = None
    """``--url``: the database URL, in place of the ``url_env`` variable."""


def render_refusal(err: FerroError) -> int:
    """Print a refusal's message to stderr and return :data:`exit_codes.REFUSED`."""
    print(err, file=sys.stderr)
    return exit_codes.REFUSED


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``ferro`` command on ``argv`` (``sys.argv[1:]`` when ``None``).

    Returns the exit code; the console script passes it to ``sys.exit``.
    """
    try:
        import cyclopts
    except ImportError:
        print(INSTALL_HINT, file=sys.stderr)
        return exit_codes.USAGE

    tokens = list(sys.argv[1:] if argv is None else argv)
    try:
        result = _app()(tokens)
    except cyclopts.CycloptsError:
        # cyclopts has already printed the error, naming the bad token.
        return exit_codes.USAGE
    return exit_codes.OK if result is None else int(result)


_built: "App | None" = None


def _app() -> "App":
    """The ``ferro`` app, built once, on first use."""
    global _built
    if _built is None:
        _built = _build()
    return _built


def _build() -> "App":
    # cyclopts resolves a command's annotations against its module's globals,
    # where ``Parameter`` (imported here, lazily) is not. This module
    # therefore does not use ``from __future__ import annotations``: the
    # launcher's annotations are evaluated when it is defined, in this scope.
    from importlib.metadata import PackageNotFoundError, version

    from cyclopts import App, Parameter

    from .migrate import migrate

    try:
        ferro_version = version("ferro-orm")
    except PackageNotFoundError:
        ferro_version = "unknown"

    root = App(
        name="ferro",
        help="ferro's command line: schema migrations for ferro models.",
        version=ferro_version,
        result_action="return_value",
        exit_on_error=False,
    )
    root.command(migrate)

    @root.meta.default
    def ferro(
        *tokens: Annotated[str, Parameter(show=False, allow_leading_hyphen=True)],
        config: Annotated[
            Path | None,
            Parameter(help="The config file to read, in place of the lookup."),
        ] = None,
        database: Annotated[
            str | None,
            Parameter(help="Which configured database the command acts on."),
        ] = None,
        url: Annotated[
            str | None,
            Parameter(help="The database URL, in place of the url_env variable."),
        ] = None,
    ) -> Any:
        command, bound, ignored = root.parse_args(tokens)
        for name, annotation in ignored.items():
            if annotation is Global:
                bound.arguments[name] = Global(
                    config=config, database=database, url=url
                )
        try:
            return command(*bound.args, **bound.kwargs)
        except FerroError as err:
            return render_refusal(err)

    meta = root.meta
    meta.result_action = "return_value"
    meta.exit_on_error = False
    return meta


def __getattr__(name: str) -> Any:
    if name == "app":
        return _app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
