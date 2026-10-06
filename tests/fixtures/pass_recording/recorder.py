"""Pytest plugin that records what auto-migrate executes and warns, per test.

Loaded with ``-p tests.fixtures.pass_recording.recorder`` by
``tests/test_pass_recording.py``, which runs the reconciliation-pass test files
in a subprocess under it. For every test it collects, in order:

- each statement auto-migrate logs before executing it (the create pass, every
  reconciliation statement including column drops, and each label addition —
  the ``Ferro Engine: auto-migrate executing on '<subject>': <sql>`` debug
  line on the ``ferro`` logger), and
- each ``ferro auto-migrate:`` warning the Rust core raises, recorded at the
  call — before any warning filter or registry can swallow it, and whether or
  not the test captures it itself, and
- each ``(statements, warnings)`` pair ``_core._render_migration_sql_for_test``
  returns — the database-free door into the same planner, which the
  row-security and check suites drive with hand-built live state.

The recording is written as JSON to ``$FERRO_PASS_RECORDING_OUT`` when the
session ends. Per-run names (Postgres test schemas, temp paths) are
normalized so a recording compares across runs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import warnings
from pathlib import Path

import pytest

STATEMENT_PREFIX = "Ferro Engine: auto-migrate executing on "
WARNING_PREFIX = "ferro auto-migrate: "
_SCHEMA_NAME = re.compile(r"ferro_[0-9a-f]{16}")


def _normalize(text: str) -> str:
    return _SCHEMA_NAME.sub("<schema>", text)


class _Recording:
    def __init__(self) -> None:
        self.by_test: dict[str, dict[str, list[str]]] = {}
        self.current: dict[str, list[str]] | None = None

    def start(self, nodeid: str) -> None:
        self.current = {"statements": [], "warnings": []}
        self.by_test[nodeid] = self.current

    def stop(self) -> None:
        self.current = None

    def statement(self, line: str) -> None:
        if self.current is not None:
            self.current["statements"].append(_normalize(line))

    def warning(self, message: str) -> None:
        if self.current is not None:
            self.current["warnings"].append(_normalize(message))

    def rendered(self, statements: list[str], warnings: list[str]) -> None:
        if self.current is not None:
            self.current.setdefault("rendered", []).append(
                {"statements": list(statements), "warnings": list(warnings)}
            )


_RECORDING = _Recording()


class _StatementHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith(STATEMENT_PREFIX):
            _RECORDING.statement(message[len(STATEMENT_PREFIX) :])


def _record_if_ferro(message: object) -> None:
    text = str(message)
    if text.startswith(WARNING_PREFIX):
        _RECORDING.warning(text[len(WARNING_PREFIX) :])


_original_warn = warnings.warn
_original_warn_explicit = warnings.warn_explicit


def _recording_warn(message, category=None, stacklevel=1, *args, **kwargs):
    _record_if_ferro(message)
    return _original_warn(message, category, stacklevel + 1, *args, **kwargs)


def _recording_warn_explicit(message, *args, **kwargs):
    _record_if_ferro(message)
    return _original_warn_explicit(message, *args, **kwargs)


def _wrap_render_migration_sql() -> None:
    from ferro import _core

    original = _core._render_migration_sql_for_test

    def recording(*args, **kwargs):
        statements, warnings_out = original(*args, **kwargs)
        _RECORDING.rendered(statements, warnings_out)
        return statements, warnings_out

    _core._render_migration_sql_for_test = recording


def pytest_configure(config: pytest.Config) -> None:
    logger = logging.getLogger("ferro")
    logger.addHandler(_StatementHandler())
    logger.setLevel(logging.DEBUG)
    # The Rust core looks `warnings.warn` / `warnings.warn_explicit` up on the
    # module at every call, so wrapping the module attributes sees every
    # warning it raises.
    warnings.warn = _recording_warn
    warnings.warn_explicit = _recording_warn_explicit
    # Test modules import the helper by name at collection, after this hook.
    _wrap_render_migration_sql()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    _RECORDING.start(item.nodeid)
    try:
        yield
    finally:
        _RECORDING.stop()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    out = os.environ.get("FERRO_PASS_RECORDING_OUT")
    if not out:
        return
    recorded = {
        nodeid: run
        for nodeid, run in sorted(_RECORDING.by_test.items())
        if run["statements"] or run["warnings"] or run.get("rendered")
    }
    Path(out).write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n")
