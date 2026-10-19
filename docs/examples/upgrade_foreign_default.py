"""Runnable companion for the Upgrade guide's foreign-default recipe
(docs/pages/upgrading.md).

A database where someone set a server default by hand on ``seen_at``, a column
the ``Account`` model declares without one. The pass reports it on every start.
"""

import asyncio
import os
import sqlite3
import tempfile
import warnings
from pathlib import Path

from upgrade_account import Account  # noqa: F401 - registers the model

import ferro

BEFORE = """
CREATE TABLE "account" (
    "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
    "name" varchar NOT NULL,
    "email" varchar NOT NULL,
    "role" varchar NOT NULL,
    "age" integer NOT NULL,
    "active" boolean NOT NULL,
    "seen_at" DATETIME DEFAULT CURRENT_TIMESTAMP,
    "prefs" JSON NOT NULL
);
CREATE UNIQUE INDEX "uq_account_email" ON "account" ("email");
"""


async def main() -> None:
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as scratch:
        connection = sqlite3.connect(Path(scratch) / "dev.db")
        connection.executescript(BEFORE)
        connection.close()
        os.chdir(scratch)
        # --8<-- [start:warning]
        await ferro.connect("sqlite:dev.db", migrate_updates=True)
        report = await ferro.migrate()
        [warning] = report.warnings
        assert "account.seen_at has default CURRENT_TIMESTAMP" in str(warning)
        # --8<-- [end:warning]
        print(warning)
        ferro.reset_engine()
        os.chdir(cwd)


if __name__ == "__main__":
    warnings.simplefilter("ignore")
    asyncio.run(main())
