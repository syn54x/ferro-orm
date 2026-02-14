import pytest
import sqlite3
import os
from typing import Annotated
from ferro import Model, connect, FerroField, ForeignKey, BackRelationship, reset_engine, clear_registry

@pytest.fixture(autouse=True)
def cleanup():
    reset_engine()
    clear_registry()
    from ferro.state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    if os.path.exists("test_1to1.db"):
        os.remove("test_1to1.db")
    yield
    if os.path.exists("test_1to1.db"):
        os.remove("test_1to1.db")

@pytest.mark.asyncio
async def test_one_to_one_relationship():
    """Verify strict 1:1 relationship behavior."""
    db_path = "test_1to1.db"
    url = f"sqlite:{db_path}?mode=rwc"

    class User(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str
        profile: BackRelationship["Profile"] = None

    class Profile(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        bio: str
        user: Annotated[User, ForeignKey(related_name="profile", unique=True)]

    await connect(url, auto_migrate=True)

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

    # 4. Verify Uniqueness Constraint in DB
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA index_list('profile')")
    indexes = cursor.fetchall()
    
    # One of the indexes should be unique for user_id
    has_unique_user_id = False
    for idx in indexes:
        idx_name = idx[1]
        cursor.execute(f"PRAGMA index_info('{idx_name}')")
        info = cursor.fetchall()
        # info rows: (seqno, cid, name)
        if any(row[2] == "user_id" for row in info) and idx[2] == 1: # idx[2] is unique flag
            has_unique_user_id = True
            break
    
    assert has_unique_user_id, "Expected unique index on profile.user_id"
    conn.close()

    # 5. Verify Uniqueness Enforcement
    with pytest.raises(Exception):
        # Should fail because alice already has a profile
        await Profile.create(bio="Another bio", user=alice)

if __name__ == "__main__":
    pytest.main([__file__])
