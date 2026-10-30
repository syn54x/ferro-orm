"""``SecretStr`` / ``SecretBytes`` fields store their real value and load back
as the secret type (#760).

A secret type only keeps a value out of ``repr``, tracebacks and logs. Every
write path binds the raw value (one unwrap helper, I-13: a query literal binds
what ``save()`` wrote), hydration wraps the decoded value back in the declared
secret type through the same per-field conversion enums use (I-2, I-6), and
``SecretBytes`` stores as binary, like ``bytes`` (I-1). Each write is checked by
reading the column with raw SQL, never through Ferro.
"""

from enum import StrEnum
from typing import Annotated

import pytest
from pydantic import SecretBytes, SecretStr, field_serializer

import ferro
from ferro import BackRef, FerroField, ForeignKey, Model, Relation
from ferro._bind_payload import (
    canonicalize_wire_scalar,
    save_bind_payload,
    update_bind_payload,
)
from ferro.raw import execute, fetch_all, fetch_one

pytestmark = pytest.mark.backend_matrix


class SFKeyring(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    passphrase: SecretStr | None = None
    accounts: Relation[list["SFAccount"]] = BackRef()


class SFAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    token: SecretStr
    blob: SecretBytes
    maybe_token: SecretStr | None = None
    maybe_blob: SecretBytes | None = None
    keyring: Annotated[SFKeyring | None, ForeignKey(related_name="accounts")] = None


class SFSerialized(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    token: SecretStr

    @field_serializer("token")
    def _show_token(self, value: SecretStr) -> str:
        return "SERIALIZED"


def _ph(db_url: str, n: int = 1) -> str:
    """The Nth positional placeholder for the active backend."""
    return f"${n}" if "postgres" in db_url else "?"


def _account(
    pk: int, *, optional: bool = True, keyring: SFKeyring | None = None
) -> SFAccount:
    return SFAccount(
        id=pk,
        keyring=keyring,
        token=SecretStr(f"hunter{pk}"),
        blob=SecretBytes(b"k\x00\xff" + bytes([pk])),
        maybe_token=SecretStr(f"maybe{pk}") if optional else None,
        maybe_blob=SecretBytes(b"m\x00" + bytes([pk])) if optional else None,
    )


async def _stored(db_url: str, pk: int) -> dict:
    row = await fetch_one(
        "SELECT token, blob, maybe_token, maybe_blob FROM sfaccount "
        f"WHERE id = {_ph(db_url)}",
        pk,
    )
    assert row is not None
    return row


def _assert_raw(row: dict, pk: int, *, optional: bool = True) -> None:
    assert row["token"] == f"hunter{pk}"
    assert bytes(row["blob"]) == b"k\x00\xff" + bytes([pk])
    if optional:
        assert row["maybe_token"] == f"maybe{pk}"
        assert bytes(row["maybe_blob"]) == b"m\x00" + bytes([pk])
    else:
        assert row["maybe_token"] is None
        assert row["maybe_blob"] is None


def _assert_hydrated(account: SFAccount, pk: int, *, optional: bool = True) -> None:
    assert type(account.token) is SecretStr
    assert account.token.get_secret_value() == f"hunter{pk}"
    assert type(account.blob) is SecretBytes
    assert account.blob.get_secret_value() == b"k\x00\xff" + bytes([pk])
    if optional:
        assert type(account.maybe_token) is SecretStr
        assert account.maybe_token.get_secret_value() == f"maybe{pk}"
        assert type(account.maybe_blob) is SecretBytes
        assert account.maybe_blob.get_secret_value() == b"m\x00" + bytes([pk])
    else:
        assert account.maybe_token is None
        assert account.maybe_blob is None


# ---------------------------------------------------------------------------
# Happy path: every write path stores the raw value; every read path rewraps.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_stores_raw_value_and_get_rewraps(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _account(1).save()
        _assert_raw(await _stored(db_url, 1), 1)
    async with ferro.engines.session():
        _assert_hydrated(await SFAccount.get(1), 1)


@pytest.mark.asyncio
async def test_bulk_create_stores_raw_value(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await SFAccount.bulk_create([_account(1), _account(2, optional=False)])
        _assert_raw(await _stored(db_url, 1), 1)
        _assert_raw(await _stored(db_url, 2), 2, optional=False)


@pytest.mark.asyncio
async def test_query_update_stores_raw_value(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _account(1, optional=False).save()
        updated = await SFAccount.where(lambda account: account.id == 1).update(
            token=SecretStr("rotated"),
            blob=SecretBytes(b"\x00new"),
            maybe_token=SecretStr("now-set"),
            maybe_blob=SecretBytes(b"\xffset"),
        )
        row = await _stored(db_url, 1)

    assert updated == 1
    assert row["token"] == "rotated"
    assert bytes(row["blob"]) == b"\x00new"
    assert row["maybe_token"] == "now-set"
    assert bytes(row["maybe_blob"]) == b"\xffset"


@pytest.mark.asyncio
async def test_all_and_first_rewrap_secret_fields(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _account(1).save()
        await _account(2, optional=False).save()
    async with ferro.engines.session():
        everyone = await SFAccount.select().order_by(lambda account: account.id).all()
        _assert_hydrated(everyone[0], 1)
        _assert_hydrated(everyone[1], 2, optional=False)
    async with ferro.engines.session():
        first = await SFAccount.select().order_by(lambda account: account.id).first()
        _assert_hydrated(first, 1)


@pytest.mark.asyncio
async def test_include_rewraps_secret_fields_on_the_related_model(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        keyring = SFKeyring(id=1, passphrase=SecretStr("open sesame"))
        await keyring.save()
        await _account(1, keyring=keyring).save()
        bare = SFKeyring(id=2)
        await bare.save()
        await _account(2, keyring=bare).save()

    async with ferro.engines.session():
        accounts = await (
            SFAccount.select()
            .include(lambda account: account.keyring)
            .order_by(lambda account: account.id)
            .all()
        )

    _assert_hydrated(accounts[0], 1)
    assert type(accounts[0].keyring.passphrase) is SecretStr
    assert accounts[0].keyring.passphrase.get_secret_value() == "open sesame"
    assert accounts[1].keyring.passphrase is None


@pytest.mark.asyncio
async def test_secret_str_literal_matches_saved_row(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _account(1).save()
        await _account(2).save()
    async with ferro.engines.session():
        by_secret = await SFAccount.where(
            lambda account: account.token == SecretStr("hunter1")
        ).all()
        by_plain = await SFAccount.where(
            lambda account: account.token == "hunter1"
        ).all()
        by_mask = await SFAccount.where(
            lambda account: account.token == "**********"
        ).all()

    assert [account.id for account in by_secret] == [1]
    assert [account.id for account in by_plain] == [1]
    assert by_mask == []


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_optional_secret_saved_as_none_stores_null_and_hydrates_none(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _account(1, optional=False).save()
        _assert_raw(await _stored(db_url, 1), 1, optional=False)
    async with ferro.engines.session():
        _assert_hydrated(await SFAccount.get(1), 1, optional=False)


@pytest.mark.asyncio
async def test_field_serializer_does_not_change_the_bound_value(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await SFSerialized(id=1, token=SecretStr("raw-value")).save()
        rows = await fetch_all("SELECT token FROM sfserialized")
    assert rows == [{"token": "raw-value"}]


@pytest.mark.asyncio
async def test_hydrated_instance_matches_constructed_instance(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    for optional in (True, False):
        pk = 1 if optional else 2
        constructed = _account(pk, optional=optional)
        async with ferro.engines.session():
            await constructed.save()
        async with ferro.engines.session():
            hydrated = await SFAccount.get(pk)

        for name in ("token", "blob", "maybe_token", "maybe_blob"):
            assert type(getattr(hydrated, name)) is type(getattr(constructed, name))
        assert hydrated.model_dump() == constructed.model_dump()
        assert hydrated.model_dump(mode="json") == constructed.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Error path: a stored value the secret type rejects names model and field.
# ---------------------------------------------------------------------------


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_stored_value_the_secret_type_rejects_names_model_and_field(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _account(1).save()
        # SQLite keeps a blob it is handed as a blob, even in a text column.
        await execute("UPDATE sfaccount SET maybe_token = X'C0FFEE' WHERE id = 1")

    async with ferro.engines.session():
        with pytest.raises(ValueError) as caught:
            await SFAccount.get(1)

    message = str(caught.value)
    assert "Failed to hydrate secret field SFAccount.maybe_token" in message
    assert "SecretStr stores str, but the column holds bytes" in message
    # The refusal never echoes the stored value: it may be the secret.
    assert "\\xc0" not in message


class SFTier(StrEnum):
    FREE = "free"


class SFPlan(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    tier: Annotated[SFTier, FerroField(db_type="text")]
    token: SecretStr | None = None


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_enum_rejection_keeps_its_message_through_the_shared_converter(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await SFPlan(id=1, tier=SFTier.FREE).save()
        await execute("UPDATE sfplan SET tier = 'gold' WHERE id = 1")

    async with ferro.engines.session():
        with pytest.raises(ValueError) as caught:
            await SFPlan.get(1)

    assert "Failed to hydrate enum field SFPlan.tier: ValueError:" in str(caught.value)
    assert SFPlan._hydration_converters["tier"] == ("enum", SFTier)
    assert SFPlan._hydration_converters["token"][0] == "secret"


# ---------------------------------------------------------------------------
# Unit pins: one unwrap for every write path (I-13), and no secret in a
# schema artifact.
# ---------------------------------------------------------------------------


def test_query_literal_and_update_bind_what_save_binds():
    account = _account(7)
    saved = save_bind_payload(account)
    updated = update_bind_payload(
        {name: getattr(account, name) for name in ("token", "blob", "maybe_blob")}
    )

    for name in ("token", "blob", "maybe_token", "maybe_blob"):
        assert canonicalize_wire_scalar(getattr(account, name)) == saved[name]
    for name in ("token", "blob", "maybe_blob"):
        assert updated[name] == saved[name]
    assert saved["token"] == "hunter7"
    assert saved["blob"] == b"k\x00\xff\x07"


class SFDefaulted(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    token: SecretStr = SecretStr("factory-token")
    blob: SecretBytes | None = SecretBytes(b"factory-blob")


def test_secret_default_is_never_a_schema_default():
    columns = SFDefaulted.__ferro_columns__
    assert columns["token"].default is None
    assert columns["blob"].default is None
    assert columns["blob"].logical_type == "binary"
