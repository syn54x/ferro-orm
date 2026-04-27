import pytest
import sqlite3
from typing import Annotated
from ferro import (
    Model,
    connect,
    FerroField,
    ForeignKey,
    BackRef,
    reset_engine,
    clear_registry,
)

pytestmark = pytest.mark.backend_matrix


@pytest.fixture(autouse=True)
def cleanup():
    reset_engine()
    clear_registry()
    from ferro.state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    yield


@pytest.mark.asyncio
async def test_one_to_one_relationship(db_url):
    """Verify strict 1:1 relationship behavior."""

    class User(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str
        profile: "Profile" = BackRef()

    class Profile(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        bio: str
        user: Annotated[User, ForeignKey(related_name="profile", unique=True)]

    await connect(db_url, auto_migrate=True)

    # 1. Create User and Profile
    alice = await User.create(username="alice")
    p1 = await Profile.create(bio="Alice's Bio", user=alice)

    # 2. Verify reverse lookup (1:1 should return object directly)
    # Note: RelationshipDescriptor returns query.first() which is a coroutine
    alice_profile = await alice.profile
    assert alice_profile is not None
    assert alice_profile.bio == "Alice's Bio"
    assert alice_profile.id == p1.id

    # 3. Verify forward lookup (already working)
    p1_user = await p1.user
    assert p1_user.username == "alice"

    # 4. Verify Uniqueness Enforcement
    with pytest.raises(Exception):
        # Should fail because alice already has a profile
        await Profile.create(bio="Another bio", user=alice)


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_one_to_one_unique_index_in_sqlite(db_url):
    class User(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str
        profile: "Profile" = BackRef()

    class Profile(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        bio: str
        user: Annotated[User, ForeignKey(related_name="profile", unique=True)]

    await connect(db_url, auto_migrate=True)

    db_path = db_url.replace("sqlite:", "").split("?")[0]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA index_list('profile')")
    indexes = cursor.fetchall()

    has_unique_user_id = False
    for idx in indexes:
        idx_name = idx[1]
        cursor.execute(f"PRAGMA index_info('{idx_name}')")
        info = cursor.fetchall()
        if any(row[2] == "user_id" for row in info) and idx[2] == 1:
            has_unique_user_id = True
            break

    conn.close()
    assert has_unique_user_id, "Expected unique index on profile.user_id"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_one_to_one_unique_index_in_postgres(
    db_url, postgres_base_url, db_schema_name
):
    class User(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str
        profile: "Profile" = BackRef()

    class Profile(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        bio: str
        user: Annotated[User, ForeignKey(related_name="profile", unique=True)]

    await connect(db_url, auto_migrate=True)

    import psycopg

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        row = conn.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = %s
              AND tablename = 'profile'
              AND indexdef ILIKE %s
            """,
            (db_schema_name, "%UNIQUE%user_id%"),
        ).fetchone()

    assert row is not None


if __name__ == "__main__":
    pytest.main([__file__])
