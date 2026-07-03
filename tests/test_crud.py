import pytest
from pydantic import Field
import ferro
from ferro import Model, ModelDoesNotExist

pytestmark = pytest.mark.backend_matrix


@pytest.mark.asyncio
async def test_model_save_new_record(db_url):
    """Test that calling .save() on a new model instance persists it to the database."""

    class CrudUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        username: str
        email: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        user = CrudUser(username="test_user", email="test@example.com")
        await user.save()
        assert user.id is not None


@pytest.mark.asyncio
async def test_model_save_update_record(db_url):
    """Test that calling .save() on an existing model instance updates it."""

    class CrudUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        username: str
        email: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        user = CrudUser(id=1, username="initial_name", email="initial@example.com")
        await user.save()
        user.username = "updated_name"
        await user.save()

        users = await CrudUser.all()
        assert len(users) == 1
        assert users[0].username == "updated_name"


@pytest.mark.asyncio
async def test_model_all_fetching(db_url):
    """Test that Model.all() retrieves all records from the database."""

    class CrudUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        username: str
        email: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        u1 = CrudUser(id=1, username="alice", email="alice@example.com")
        u2 = CrudUser(id=2, username="bob", email="bob@example.com")
        await u1.save()
        await u2.save()
        users = await CrudUser.all()
        assert len(users) == 2
        assert any(u.username == "alice" for u in users)
        assert any(u.username == "bob" for u in users)


@pytest.mark.asyncio
async def test_upsert_updates_existing_row_without_duplicate(db_url):
    """Plain save() of a duplicate PK raises; the explicit upsert() updates (FF-A A3/A4)."""

    class CrudUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        username: str
        email: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        user = CrudUser(id=42, username="original", email="original@example.com")
        await user.save()
        users_before = await CrudUser.all()
        assert len(users_before) == 1

        with pytest.raises(ferro.UniqueViolationError):
            await CrudUser(id=42, username="updated", email="original@example.com").save()

        await CrudUser.upsert(id=42, username="updated", email="original@example.com")
    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        fetched = await CrudUser.get(42)
        assert fetched is not None
        assert fetched.username == "updated"
        assert len(await CrudUser.all()) == 1


@pytest.mark.asyncio
async def test_identity_map_consistency(db_url):
    """Test that fetching the same record twice returns the same Python object instance."""

    class CrudUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        username: str
        email: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        u1 = CrudUser(id=100, username="identity", email="id@test.com")
        await u1.save()
        results_1 = await CrudUser.all()
        results_2 = await CrudUser.all()
        user_a = results_1[0]
        user_b = results_2[0]
        assert user_a is user_b
        assert user_a.id == 100


@pytest.mark.asyncio
async def test_identity_map_disabled_returns_distinct_instances(db_url):
    """With identity_map=False, repeated loads are distinct objects (same field values)."""

    class CrudUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        username: str
        email: str

    await ferro.connect(db_url, auto_migrate=True, identity_map=False)
    async with ferro.engines.session():

        u1 = CrudUser(id=200, username="nomap", email="nomap@test.com")
        await u1.save()
        results_1 = await CrudUser.all()
        results_2 = await CrudUser.all()
        user_a = results_1[0]
        user_b = results_2[0]
        assert user_a is not user_b
        assert user_a.id == user_b.id == 200
        assert user_a.username == user_b.username


@pytest.mark.asyncio
async def test_model_get_operation(db_url):
    """Test fetching a single record by primary key."""

    class CrudUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        username: str
        email: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        u1 = CrudUser(id=500, username="get_test", email="get@test.com")
        await u1.save()
        user = await CrudUser.get(500)
        assert user is not None
        assert user.id == 500
        assert user is u1


@pytest.mark.asyncio
async def test_model_get_invalid_usage(db_url):
    """Test that get() raises error with invalid arguments."""

    class CrudUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        username: str
        email: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        with pytest.raises(TypeError):
            await CrudUser.get(id=1)
        with pytest.raises(TypeError):
            await CrudUser.get()


@pytest.mark.asyncio
async def test_model_get_not_found(db_url):
    """Test that get() raises when the record does not exist."""

    class CrudUser(Model):
        id: int = Field(default=None, json_schema_extra={"primary_key": True})
        username: str
        email: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        with pytest.raises(ModelDoesNotExist) as exc_info:
            await CrudUser.get(9999)
        assert exc_info.value.model is CrudUser
        assert exc_info.value.pk == 9999
        assert await CrudUser.get_or_none(9999) is None
