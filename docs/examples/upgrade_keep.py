"""Runnable companion for the Upgrade guide's keep recipes
(docs/pages/upgrading.md).

``Account`` with the three fields that opt out of a derived storage type that
changed: a naive timestamp, an enum stored as text with a check, and plain-JSON
storage. The Annotated twin is ``upgrade_keep_annotated.py``.
"""

import asyncio
from datetime import datetime
from enum import StrEnum

from ferro import Field, Model, connect, create_tables, engines


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class Account(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    # --8<-- [start:keep-timestamp]
    seen_at: datetime | None = Field(default=None, db_type="timestamp")
    # --8<-- [end:keep-timestamp]
    # --8<-- [start:keep-varchar]
    role: Role = Field(default=Role.MEMBER, db_type="text", db_check=True)
    # --8<-- [end:keep-varchar]
    # --8<-- [start:keep-json]
    prefs: dict = Field(default_factory=dict, db_type="json")
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
