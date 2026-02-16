from typing import Annotated, ForwardRef

import pytest

from ferro import (
    BackRef,
    FerroField,
    Field,
    ForeignKey,
    Model,
    clear_registry,
    reset_engine,
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
    posts: BackRef[list["Post"]] = None


class Post(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: str
    # Forward link
    author: Annotated[User, ForeignKey(related_name="posts")]


def test_metadata_discovery():
    """Verify that the Metaclass finds ForeignKey and BackRef annotations."""
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
    assert User.ferro_relations["posts"] == "BackRef"
    # Check if it's a descriptor
    assert hasattr(User, "posts")


def test_pydantic_isolation():
    """Verify that BackRef fields are not required by Pydantic."""
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
        posts: BackRef[list[Post]] = None

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
        wrong_name: BackRef[list["Post"]] = None

    class Post(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        author: Annotated[User, ForeignKey(related_name="posts")]

    from ferro.relations import resolve_relationships

    with pytest.raises(
        Exception, match="defines a relationship to 'User' with related_name='posts'"
    ):
        resolve_relationships()


def test_back_ref_via_field_default():
    """Field(default=None, back_ref=True) declares a reverse relation like BackRef."""

    class UserViaField(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str
        posts: list["PostViaField"] | None = Field(default=None, back_ref=True)

    class PostViaField(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author: Annotated[UserViaField, ForeignKey(related_name="posts")]

    assert "posts" in UserViaField.ferro_relations
    assert UserViaField.ferro_relations["posts"] == "BackRef"

    from ferro.relations import resolve_relationships

    resolve_relationships()
    assert hasattr(UserViaField, "posts")


def test_back_ref_via_annotated_field():
    """Annotated[list["Post"], Field(back_ref=True)] = None declares a reverse relation."""

    class UserAnnotated(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str
        posts: Annotated[list["PostAnnotated"] | None, Field(back_ref=True)] = None

    class PostAnnotated(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author: Annotated[UserAnnotated, ForeignKey(related_name="posts")]

    assert "posts" in UserAnnotated.ferro_relations
    assert UserAnnotated.ferro_relations["posts"] == "BackRef"

    from ferro.relations import resolve_relationships

    resolve_relationships()
    assert hasattr(UserAnnotated, "posts")


def test_back_ref_and_field_back_ref_raises():
    """Cannot use both BackRef and Field(back_ref=True) on the same field."""

    with pytest.raises(
        TypeError,
        match="Cannot use both BackRef and Field\\(back_ref=True\\) on the same field 'posts'",
    ):

        class UserDouble(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            username: str
            posts: BackRef[list["PostDouble"]] = Field(default=None, back_ref=True)

        class PostDouble(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            author: Annotated[UserDouble, ForeignKey(related_name="posts")]
