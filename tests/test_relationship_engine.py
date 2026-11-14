from typing import Annotated, ClassVar

import pytest

from ferro import (
    FerroField,
    Forward,
    Model,
    Relation,
    Reverse,
    clear_registry,
    reset_engine,
)
from ferro.base import ForwardRelation, ReverseRelation


class RelationshipError(Exception):
    """Custom error for relationship validation failures."""

    pass


@pytest.fixture(autouse=True)
def cleanup():
    from ferro.registry import REGISTRY

    REGISTRY.reset_for_test()
    reset_engine()
    clear_registry()
    yield


class User(Model):
    # Module-scope fixture model: defined once at import time, so it is part
    # of every test's registry baseline for the whole session (conftest's
    # _ferro_registry_isolation only snapshots/restores, it never clears).
    # Explicit __ferro_table__ names keep it from squatting the generic
    # "user"/"post" default table names that many other test files'
    # function-local models rely on (FF-E E1 table-name collision detection).
    __ferro_table__: ClassVar[str] = "relationship_engine_user"
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    username: str
    # Reverse relation
    posts: Relation[list["Post"]] = Reverse("author_id")


class Post(Model):
    __ferro_table__: ClassVar[str] = "relationship_engine_post"
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: str
    # Forward relation over its key column
    author_id: Annotated[int, FerroField(foreign_key=User)]
    author: Relation[User] = Forward("author_id")


def test_many_to_many_field_import_removed_from_public_api():
    """ManyToManyField is no longer exported as public API."""
    import ferro

    assert not hasattr(ferro, "ManyToManyField")


def test_metadata_discovery():
    """Verify that the Metaclass finds the forward and reverse relations."""
    # Before resolution
    assert "author" in Post.__ferro_relations__
    assert (
        "author_id" in Post.__ferro_query_columns__
    )  # the declared foreign-key column

    # After resolution
    from ferro.relations import resolve_relationships

    resolve_relationships()

    assert isinstance(Post.__ferro_relations__["author"], ForwardRelation)
    assert Post.__ferro_relations__["author"].fk_column == "author_id"

    # The foreign-key column is a declared model field
    assert "author_id" in Post.model_fields

    # Verify discovery on User
    assert "posts" in User.__ferro_relations__
    assert isinstance(User.__ferro_relations__["posts"], ReverseRelation)
    assert User.__ferro_relations__["posts"].attribute == "author_id"
    # Check if it's a descriptor
    assert hasattr(User, "posts")
