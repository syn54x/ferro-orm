"""Runnable companion for the Upgrade guide's keep recipes
(docs/pages/upgrading.md).

``Account`` with the three fields that opt out of a derived storage type that
changed: a naive timestamp, an enum stored as text with a check, and plain-JSON
storage. The assignment twin is ``upgrade_keep.py``.
"""

import asyncio
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from ferro import Field, Model, connect, create_tables, engines


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class Account(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: str
    email: Annotated[str, Field(unique=True)]
    # --8<-- [start:keep-timestamp]
    seen_at: Annotated[datetime | None, Field(db_type="timestamp")] = None
    # --8<-- [end:keep-timestamp]
    # --8<-- [start:keep-varchar]
    role: Annotated[Role, Field(db_type="text", db_check=True)] = Role.MEMBER
    # --8<-- [end:keep-varchar]
    # --8<-- [start:keep-json]
    prefs: Annotated[dict, Field(db_type="json", default_factory=dict)]
    # --8<-- [end:keep-json]


async def main() -> None:
    await connect("sqlite::memory:")
    report = await create_tables()
    [statement] = [
        s.sql
        for s in report.statements
        if s.sql.startswith('CREATE TABLE IF NOT EXISTS "account"')
    ]
    assert '"role" text NOT NULL CONSTRAINT "ck_account_role" CHECK' in statement
    async with engines.session():
        ada = await Account.create(name="Ada", email="ada@example.com")
        assert ada.role is Role.MEMBER


if __name__ == "__main__":
    asyncio.run(main())
