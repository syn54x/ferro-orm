"""A declared binary column over a live text column (a 0.25 ``SecretBytes``
column was ``varchar`` on Postgres), or the reverse, refuses on Postgres.

Postgres coerces a ``bytea`` bind into a ``varchar`` column through the type's
I/O functions and stores the hex text, so ``b"\\xff\\x00k"`` came back as the
seven characters ``\\xff006b`` with no error. The per-table catalog now records
each column's live storage class, and a write or a fetch that would cross the
text/bytes boundary raises ``DataError`` before any statement is sent (#785).
SQLite stores the bytes as written, so the same model round-trips there.
"""

from typing import Annotated

import pytest
from pydantic import SecretBytes, SecretStr

import ferro
from ferro import BackRef, FerroField, ForeignKey, Model, Relation
from ferro._core import _catalog_query_count_for_test
from ferro.exceptions import DataError
from ferro.models import _FERRO_PERSISTED_ATTR
from ferro.raw import execute, fetch_all

RAW = b"\xff\x00k"
REFUSAL = "Ferro will not write bytes into a text column \\(or read text as bytes\\)"
OLD_TOKEN = 'CREATE TABLE "token" ("id" integer PRIMARY KEY, "secret" varchar NOT NULL)'


@pytest.fixture(autouse=True)
def _fresh_registry(clean_registry):
    yield


def _token_model(*, nullable: bool = False):
    if nullable:

        class Token(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            secret: SecretBytes | None = None

    else:

        class Token(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            secret: SecretBytes

    return Token


async def _connect_with_table(db_url: str, ddl: str) -> None:
    """Connect without migrating and build the table by hand, as 0.25 did."""
    await ferro.connect(db_url)
    async with ferro.engines.session():
        await execute(ddl)


async def _count(table: str) -> int:
    rows = await fetch_all(f'SELECT count(*) AS n FROM "{table}"')
    return rows[0]["n"]


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_matching_bytea_round_trips_with_no_extra_catalog_query(db_url):
    Token = _token_model()
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await Token(id=1, secret=SecretBytes(RAW)).save()
        baseline = _catalog_query_count_for_test()
        await Token(id=2, secret=SecretBytes(RAW)).save()
        fetched = await Token.get(1)
        assert fetched.secret.get_secret_value() == RAW
        assert await Token.where(lambda token: token.id > 0).count() == 2
        assert _catalog_query_count_for_test() == baseline


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_save_refuses_and_leaves_the_table_unchanged(db_url):
    Token = _token_model()
    await _connect_with_table(db_url, OLD_TOKEN)
    async with ferro.engines.session():
        with pytest.raises(DataError) as raised:
            await Token(id=1, secret=SecretBytes(RAW)).save()
        assert str(raised.value) == (
            "Token.secret is declared bytea but the live column is varchar; "
            "Ferro will not write bytes into a text column (or read text as bytes). "
            "Change the column's type with a migration (ferro migrate new) or "
            "connect with migrate_updates=True."
        )
        assert await _count("token") == 0


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_bulk_create_instance_update_and_query_update_refuse(db_url):
    Token = _token_model()
    await _connect_with_table(db_url, OLD_TOKEN)
    async with ferro.engines.session():
        await execute("INSERT INTO \"token\" VALUES (1, 'plain')")

        with pytest.raises(DataError, match="Token.secret"):
            await Token.bulk_create(
                [
                    Token(id=2, secret=SecretBytes(RAW)),
                    Token(id=3, secret=SecretBytes(RAW)),
                ]
            )

        persisted = Token(id=1, secret=SecretBytes(RAW))
        persisted.__dict__[_FERRO_PERSISTED_ATTR] = True
        with pytest.raises(DataError, match="Token.secret"):
            await persisted.save()

        with pytest.raises(DataError, match="Token.secret"):
            await Token.where(lambda token: token.id == 1).update(secret=RAW)

        rows = await fetch_all('SELECT id, secret FROM "token"')
        assert [(row["id"], row["secret"]) for row in rows] == [(1, "plain")]


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_fetches_that_select_the_column_refuse(db_url):
    Token = _token_model()
    await _connect_with_table(db_url, OLD_TOKEN)
    async with ferro.engines.session():
        await execute("INSERT INTO \"token\" VALUES (1, 'plain')")

        with pytest.raises(DataError, match=REFUSAL):
            await Token.get(1)
        with pytest.raises(DataError, match=REFUSAL):
            await Token.where(lambda token: token.id == 1).all()
        with pytest.raises(DataError, match=REFUSAL):
            await Token.where(lambda token: token.id == 1).first()
        with pytest.raises(DataError, match=REFUSAL):
            await Token.select(lambda token: token.secret).all()

        ids = await Token.select(lambda token: token.id).all()
        assert [row.id for row in ids] == [1]


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_text_column_over_live_bytea_refuses_both_doors(db_url):
    class Note(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        body: SecretStr

    await _connect_with_table(
        db_url, 'CREATE TABLE "note" ("id" integer PRIMARY KEY, "body" bytea NOT NULL)'
    )
    async with ferro.engines.session():
        await execute("INSERT INTO \"note\" VALUES (1, '\\x6162')")
        with pytest.raises(
            DataError,
            match="Note.body is declared varchar but the live column is bytea",
        ):
            await Note(id=2, body=SecretStr("ab")).save()
        with pytest.raises(DataError, match=REFUSAL):
            await Note.get(1)
        assert await _count("note") == 1


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_saving_none_into_a_nullable_mismatched_column_refuses(db_url):
    Token = _token_model(nullable=True)
    await _connect_with_table(
        db_url, 'CREATE TABLE "token" ("id" integer PRIMARY KEY, "secret" varchar)'
    )
    async with ferro.engines.session():
        with pytest.raises(DataError, match=REFUSAL):
            await Token(id=1, secret=None).save()
        assert await _count("token") == 0


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_include_of_a_mismatched_target_refuses_but_the_root_alone_loads(db_url):
    class Vault(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        secret: SecretBytes
        keys: Relation[list["Key"]] = BackRef()

    class Key(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        vault: Annotated[Vault | None, ForeignKey(related_name="keys")] = None

    await ferro.connect(db_url)
    async with ferro.engines.session():
        await execute(
            'CREATE TABLE "vault" ("id" integer PRIMARY KEY, "secret" varchar NOT NULL)'
        )
        await execute(
            'CREATE TABLE "key" ("id" integer PRIMARY KEY, '
            '"vault_id" integer REFERENCES "vault" ("id"))'
        )
        await execute("INSERT INTO \"vault\" VALUES (1, 'plain')")
        await execute('INSERT INTO "key" VALUES (1, 1)')

        assert [key.id for key in await Key.select().all()] == [1]
        with pytest.raises(DataError, match="Vault.secret"):
            await Key.select().include(lambda key: key.vault).all()


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_migrate_updates_clears_the_cached_class_on_the_same_engine(db_url):
    Token = _token_model()
    await _connect_with_table(db_url, OLD_TOKEN)
    async with ferro.engines.session():
        with pytest.raises(DataError, match=REFUSAL):
            await Token(id=1, secret=SecretBytes(RAW)).save()

    await ferro.migrate(updates=True)

    async with ferro.engines.session():
        await Token(id=1, secret=SecretBytes(RAW)).save()
        assert (await Token.get(1)).secret.get_secret_value() == RAW


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_sqlite_stores_the_bytes_over_a_varchar_column(db_url):
    Token = _token_model()
    await _connect_with_table(db_url, OLD_TOKEN)
    async with ferro.engines.session():
        await Token(id=1, secret=SecretBytes(RAW)).save()
        assert (await Token.get(1)).secret.get_secret_value() == RAW


@pytest.mark.asyncio
@pytest.mark.postgres_only
@pytest.mark.parametrize("live", ["varchar", "bytea"])
async def test_projection_through_a_relation_checks_the_hop_column(db_url, live):
    class Vault(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        secret: SecretBytes
        keys: Relation[list["Key"]] = BackRef()

    class Key(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        vault: Annotated[Vault | None, ForeignKey(related_name="keys")] = None

    await ferro.connect(db_url)
    async with ferro.engines.session():
        await execute(
            f'CREATE TABLE "vault" ("id" integer PRIMARY KEY, "secret" {live} NOT NULL)'
        )
        await execute(
            'CREATE TABLE "key" ("id" integer PRIMARY KEY, '
            '"vault_id" integer REFERENCES "vault" ("id"))'
        )
        await execute(
            'INSERT INTO "vault" VALUES (1, '
            + ("'\\xff006b')" if live == "varchar" else "'\\xff006b'::bytea)")
        )
        await execute('INSERT INTO "key" VALUES (1, 1)')

        query = Key.select(lambda key: key.vault.secret)
        if live == "varchar":
            with pytest.raises(DataError, match="Vault.secret"):
                await query.all()
        else:
            assert len(await query.all()) == 1
