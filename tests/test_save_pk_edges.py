"""FF-G G4a: legitimate non-positive primary keys round-trip through save().

The Postgres RETURNING decode used to discard ids <= 0
(`(id > 0).then_some(id)`), leaving the instance PK None."""

from typing import Annotated, Optional

import pytest

import ferro
from ferro import Model, transaction
from ferro.base import FerroField
from ferro.raw import execute


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_sequence_generated_zero_pk_round_trips(db_url):
    """Covers the non-tx-connection RETURNING arm (the `None` `tx_conn` case)."""

    class SeqZeroPk(Model):
        id: Annotated[Optional[int], FerroField(primary_key=True)] = None
        name: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        # serial PK owns sequence <table>_<col>_seq; make it emit 0 next.
        await execute('ALTER SEQUENCE "seqzeropk_id_seq" MINVALUE 0 RESTART WITH 0')
        row = SeqZeroPk(name="zero")
        await row.save()
        assert row.id == 0
        fetched = await SeqZeroPk.get(0)
        assert fetched.name == "zero"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_sequence_generated_negative_pk_round_trips_in_transaction(db_url):
    """Covers the tx-connection RETURNING arm (the other `then_some` site)."""

    class SeqNegPk(Model):
        id: Annotated[Optional[int], FerroField(primary_key=True)] = None
        name: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await execute('ALTER SEQUENCE "seqnegpk_id_seq" MINVALUE -100 RESTART WITH -5')
        async with transaction():
            row = SeqNegPk(name="negative")
            await row.save()
            assert row.id == -5
        fetched = await SeqNegPk.get(-5)
        assert fetched.name == "negative"
