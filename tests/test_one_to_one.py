import pytest
from typing import Annotated
from ferro import (
    Model,
    connect,
    engines,
    FerroField,
    Forward,
    Relation,
    Reverse,
    reset_engine,
    clear_registry,
)

pytestmark = pytest.mark.backend_matrix


@pytest.fixture(autouse=True)
def cleanup():
    reset_engine()
    clear_registry()
    from ferro.registry import REGISTRY

    REGISTRY.reset_for_test()
    yield


@pytest.mark.asyncio
async def test_one_to_one_relationship(db_url):
    """Verify strict 1:1 relationship behavior."""

    class User(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str
        profile: Relation[list["Profile"]] = Reverse("user_id")

    class Profile(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        bio: str
        user_id: Annotated[int, FerroField(foreign_key=User, unique=True)]
        user: Relation[User] = Forward("user_id")

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        # 1. Create User and Profile
        alice = await User.create(username="alice")
        p1 = await Profile.create(bio="Alice's Bio", user_id=alice.id)

        # 2. Verify reverse lookup: a one-to-one reverse relation is a
        # query read with .first()
        alice_profile = await alice.profile.first()
        assert alice_profile is not None
        assert alice_profile.bio == "Alice's Bio"
        assert alice_profile.id == p1.id

        # 3. Verify forward lookup: the session's alice, with no query
        p1_user = p1.user
        assert p1_user is alice
        assert p1_user.username == "alice"

        # 4. Verify Uniqueness Enforcement
        with pytest.raises(Exception):
            # Should fail because alice already has a profile
            await Profile.create(bio="Another bio", user_id=alice.id)


if __name__ == "__main__":
    pytest.main([__file__])
