"""Runnable companion to the Inspecting SQL guide (docs/pages/guide/inspecting-sql.md).

Every block on the page uses the ``Account`` model this script declares. The
page's output blocks are regenerated from the regions below
(``tests/test_docs_output.py``), and its SQL blocks from the statements they
send (``tests/test_docs_sql.py``).
"""

import asyncio
import logging
from typing import Annotated

from pydantic import SecretStr

from ferro import Field, Model


# --8<-- [start:models]
class Account(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    owner: str
    token: SecretStr


# --8<-- [end:models]


def untimed(record: logging.LogRecord) -> str:
    """Return the record's message without its trailing timing, which changes on every run."""
    return record.getMessage().rsplit(" ", 1)[0]


async def main() -> None:
    # --8<-- [start:statement-log]
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("ferro.sql")
    logger.addHandler(Collect())
    logger.setLevel(logging.DEBUG)
    # --8<-- [end:statement-log]

    # --8<-- [start:first]
    from ferro import connect, engines, transaction

    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        rendered = await Account.where(lambda account: account.owner == "Ada").all.sql()
        print(rendered)
        print(rendered.params)
        # --8<-- [end:first]
        assert rendered.params == ("Ada",)

        # --8<-- [start:a-record]
        await Account(owner="Ada", token=SecretStr("sk_live_ada")).save()

        print(untimed(records[-1]))
        # --8<-- [end:a-record]
        assert records[-1].operation == "save"  # type: ignore[attr-defined]

        # --8<-- [start:attributes]
        record = records[-1]
        for name in ("sql", "params", "dialect", "operation", "subject", "connection"):
            print(f"{name}: {getattr(record, name)!r}")
        print("rows:", record.rows)  # type: ignore[attr-defined]
        # --8<-- [end:attributes]

        # --8<-- [start:operations]
        print(list(dict.fromkeys(record.operation for record in records)))  # type: ignore[attr-defined]

        records.clear()
        async with transaction():
            await Account(owner="Grace", token=SecretStr("sk_live_grace")).save()
            await Account.where(lambda account: account.owner == "Grace").all()
        print([record.operation for record in records])  # type: ignore[attr-defined]
        # --8<-- [end:operations]

        # --8<-- [start:count-and-update]
        counted = await Account.select().count.sql()
        print(counted)

        renamed = await Account.where(
            lambda account: account.owner == "Ada"
        ).update.sql(owner="Ada Lovelace")
        print(renamed)
        print(renamed.params)
        # --8<-- [end:count-and-update]

        # --8<-- [start:secrets]
        looked_up = await Account.where(
            lambda account: account.token == "sk_live_ada"
        ).all.sql()
        print(looked_up.params)

        records.clear()
        await Account.where(lambda account: account.token == "sk_live_ada").all()
        print(untimed(records[-1]))
        # --8<-- [end:secrets]

        # --8<-- [start:assert-on-records]
        records.clear()
        await Account.where(lambda account: account.owner == "Grace").delete()
        deletes = [record for record in records if record.operation == "delete"]  # type: ignore[attr-defined]
        assert len(deletes) == 1
        assert deletes[0].params == ("Grace",)  # type: ignore[attr-defined]
        # --8<-- [end:assert-on-records]

    print("inspecting_sql example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
