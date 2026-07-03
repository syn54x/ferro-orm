"""create() is a real INSERT; save() distinguishes INSERT from UPDATE (FF-A A3/A4, #173/#174).

Exit-gate rule: exception *types* (and structured attributes) are asserted —
never driver message text.
"""

from typing import Annotated
from uuid import UUID, uuid4

import pytest

import ferro
from ferro import (
    FerroField,
    IntegrityError,
    Model,
    ModelDoesNotExist,
    UniqueViolationError,
    connect,
)

pytestmark = [pytest.mark.backend_matrix, pytest.mark.asyncio]


async def test_create_duplicate_pk_raises_unique_violation(db_url, db_backend):
    """Exit gate: create() on an existing PK raises UniqueViolationError, no clobber."""

    class CreateInsertUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await CreateInsertUser.create(id=1, name="first")

        with pytest.raises(UniqueViolationError) as excinfo:
            await CreateInsertUser.create(id=1, name="second")

        exc = excinfo.value
        assert isinstance(exc, IntegrityError)
        if db_backend == "postgres":
            assert exc.sqlstate == "23505"

        rows = await CreateInsertUser.all()
        assert len(rows) == 1
        assert rows[0].name == "first"


async def test_save_transient_instance_with_taken_pk_raises(db_url):
    class TransientPkUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await TransientPkUser(id=1, name="original").save()

        with pytest.raises(UniqueViolationError):
            await TransientPkUser(id=1, name="intruder").save()

        rows = await TransientPkUser.all()
        assert len(rows) == 1
        assert rows[0].name == "original"


async def test_save_persisted_instance_is_update(db_url):
    class PersistedUpdateUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        user = await PersistedUpdateUser.create(id=1, name="before")
        user.name = "after"
        await user.save()

        rows = await PersistedUpdateUser.all()
        assert len(rows) == 1

    ferro.reset_engine()
    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        fetched = await PersistedUpdateUser.get(1)
        assert fetched.name == "after"


async def test_save_twice_on_same_transient_instance_updates(db_url):
    class DoubleSaveUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        user = DoubleSaveUser(name="a")
        await user.save()
        assert user.id is not None
        user.name = "b"
        await user.save()

        rows = await DoubleSaveUser.all()
        assert len(rows) == 1
        assert rows[0].name == "b"


async def test_fetched_instance_save_is_update(db_url):
    """A hydrated instance carries persistence state from the Rust core."""

    class HydratedSaveUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await HydratedSaveUser.create(id=7, name="cold")

    ferro.reset_engine()
    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        fetched = await HydratedSaveUser.get(7)
        fetched.name = "warm"
        await fetched.save()

        rows = await HydratedSaveUser.all()
        assert len(rows) == 1
        assert rows[0].name == "warm"


async def test_save_after_row_deleted_raises_model_does_not_exist(db_url):
    class StaleRowUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        user = await StaleRowUser.create(id=3, name="here")
        await StaleRowUser.where(lambda u: u.id == 3).delete()

        user.name = "gone"
        with pytest.raises(ModelDoesNotExist) as excinfo:
            await user.save()

        assert excinfo.value.model is StaleRowUser
        assert excinfo.value.pk == 3


async def test_pk_mutation_on_persisted_instance_targets_current_pk(db_url):
    """Documented limitation: UPDATE targets the current PK value; a mutated
    PK matches no row and raises instead of silently inserting (FF-D will add
    real PK-change tracking)."""

    class PkMutationUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        user = await PkMutationUser.create(id=1, name="a")
        user.id = 999

        with pytest.raises(ModelDoesNotExist) as excinfo:
            await user.save()
        assert excinfo.value.pk == 999

    # Assert durable state across a reconnect: the identity map would
    # otherwise hand back the same live object whose PK the test mutated.
    ferro.reset_engine()
    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        rows = await PkMutationUser.all()
        assert len(rows) == 1
        assert rows[0].id == 1
        assert rows[0].name == "a"


async def test_upsert_inserts_when_row_missing(db_url):
    class UpsertInsertDoc(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        doc = await UpsertInsertDoc.upsert(id=7, label="a")

        rows = await UpsertInsertDoc.all()
        assert len(rows) == 1
        assert rows[0].label == "a"

        # The returned instance is persisted: a follow-up save() is an UPDATE.
        doc.label = "b"
        await doc.save()
        rows = await UpsertInsertDoc.all()
        assert len(rows) == 1
        assert rows[0].label == "b"


async def test_upsert_updates_existing_row(db_url):
    class UpsertUpdateDoc(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await UpsertUpdateDoc.create(id=7, label="a")
        await UpsertUpdateDoc.upsert(id=7, label="b")

        rows = await UpsertUpdateDoc.all()
        assert len(rows) == 1
        assert rows[0].label == "b"


async def test_save_on_conflict_update_primitive(db_url):
    """save(on_conflict="update") is the primitive behind Model.upsert()."""

    class OnConflictDoc(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await OnConflictDoc.create(id=1, label="a")

        await OnConflictDoc(id=1, label="b").save(on_conflict="update")

        rows = await OnConflictDoc.all()
        assert len(rows) == 1
        assert rows[0].label == "b"


async def test_save_rejects_unknown_on_conflict_value(db_url):
    class BadOnConflictDoc(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        with pytest.raises(ValueError):
            await BadOnConflictDoc(id=1, label="a").save(on_conflict="ignore")

        rows = await BadOnConflictDoc.all()
        assert len(rows) == 0


async def test_upsert_with_unset_auto_pk_inserts(db_url):
    """An autoincrement PK left unset has no conflict target: plain INSERT."""

    class AutoPkUpsertDoc(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        doc = await AutoPkUpsertDoc.upsert(label="x")

        assert doc.id is not None
        rows = await AutoPkUpsertDoc.all()
        assert len(rows) == 1


async def test_delete_returns_instance_to_transient(db_url):
    class DeleteResaveDoc(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        doc = await DeleteResaveDoc.create(id=4, label="v1")
        await doc.delete()
        assert len(await DeleteResaveDoc.all()) == 0

        # A deleted instance is transient again: save() re-INSERTs.
        await doc.save()
        rows = await DeleteResaveDoc.all()
        assert len(rows) == 1
        assert rows[0].id == 4


async def test_refresh_keeps_instance_persisted(db_url):
    class RefreshDoc(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        doc = await RefreshDoc.create(id=5, label="a")
        await doc.refresh()
        doc.label = "b"
        await doc.save()

        rows = await RefreshDoc.all()
        assert len(rows) == 1
        assert rows[0].label == "b"


async def test_model_copy_of_persisted_instance_updates_same_row(db_url):
    class CopySaveDoc(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        doc = await CopySaveDoc.create(id=6, label="a")

        copy = doc.model_copy()
        copy.label = "b"
        await copy.save()

        rows = await CopySaveDoc.all()
        assert len(rows) == 1
        assert rows[0].label == "b"


async def test_nonauto_pk_insert_then_duplicate_raises(db_url):
    class UuidPkDoc(Model):
        id: Annotated[UUID | None, FerroField(primary_key=True)] = None
        label: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        pk = uuid4()
        await UuidPkDoc(id=pk, label="first").save()

        with pytest.raises(UniqueViolationError):
            await UuidPkDoc(id=pk, label="dup").save()

        await UuidPkDoc.upsert(id=pk, label="second")
        rows = await UuidPkDoc.all()
        assert len(rows) == 1
        assert rows[0].label == "second"


@pytest.mark.sqlite_only
async def test_model_connection_upsert(tmp_path):
    class ConnUpsertMarker(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"
    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")
    async with ferro.engines.session():
        await ferro.create_tables()
        await ferro.create_tables(using="service")

        await ConnUpsertMarker.create(id=1, label="app")

        # `using="service"` conflicts with the ambient "app" session (D4);
        # a nested `engines.session("service")` gives it a matching ambient.
        async with ferro.engines.session("service"):
            await ConnUpsertMarker.using("service").create(id=1, label="service")
            await ConnUpsertMarker.using("service").upsert(id=1, label="service-v2")

        app_row = await ConnUpsertMarker.get(1)
        async with ferro.engines.session("service"):
            service_row = await ConnUpsertMarker.using("service").get(1)
        assert app_row.label == "app"
        assert service_row.label == "service-v2"
