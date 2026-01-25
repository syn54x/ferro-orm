import pytest
from typing import Annotated, ForwardRef
from ferro import (
    Model,
    FerroField,
    reset_engine,
    clear_registry,
    ForeignKey,
    BackRelationship,
)


class RelationshipError(Exception):
    """Custom error for relationship validation failures."""

    pass


@pytest.fixture(autouse=True)
def cleanup():
    from ferro.state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    reset_engine()
    clear_registry()
    yield


class User(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    username: str
    # Reverse marker
    posts: BackRelationship[list["Post"]] = None


class Post(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: str
    # Forward link
    author: Annotated[User, ForeignKey(related_name="posts")]


def test_metadata_discovery():
    """Verify that the Metaclass finds ForeignKey and BackRelationship annotations."""
    # Before resolution
    assert "author" in Post.ferro_relations
    assert Post.author_id is not None  # FieldProxy

    # After resolution
    from ferro.relations import resolve_relationships

    resolve_relationships()

    assert isinstance(Post.ferro_relations["author"], ForeignKey)
    assert Post.ferro_relations["author"].related_name == "posts"

    # Verify shadow field generation
    assert "author_id" in Post.model_fields

    # Verify discovery on User
    assert "posts" in User.ferro_relations
    assert User.ferro_relations["posts"] == "BackRelationship"
    # Check if it's a descriptor
    assert hasattr(User, "posts")


def test_pydantic_isolation():
    """Verify that BackRelationship fields are not required by Pydantic."""
    # Should NOT raise an error about 'posts' missing
    u = User(username="alice")
    assert u.username == "alice"


@pytest.mark.asyncio
async def test_forward_ref_resolution():
    """Verify that string/ForwardRef model references are resolved during connect()."""

    # 1. Define referencing model first
    class Post(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        author: Annotated["Author", ForeignKey(related_name="posts")]

    # 2. Define target model second
    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        posts: BackRelationship[list[Post]] = None

    # Initially it's a string/ForwardRef
    raw_to = Post.ferro_relations["author"].to
    assert isinstance(raw_to, (str, ForwardRef))

    # 3. Simulate connect() / resolution
    from ferro.relations import resolve_relationships

    resolve_relationships()

    # Now it should be the class
    assert Post.ferro_relations["author"].to is Author


def test_relationship_validation_failure():
    """Verify that an error is raised if related_name doesn't match a field."""

    class User(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str
        # WRONG NAME HERE
        wrong_name: BackRelationship[list["Post"]] = None

    class Post(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        author: Annotated[User, ForeignKey(related_name="posts")]

    from ferro.relations import resolve_relationships

    with pytest.raises(
        Exception, match="defines a relationship to 'User' with related_name='posts'"
    ):
        resolve_relationships()
