from typing import Annotated, ClassVar, ForwardRef

import pytest

from ferro import (
    BackRef,
    FerroField,
    Field,
    ForeignKey,
    Forward,
    ManyToMany,
    Model,
    Relation,
    Reverse,
    clear_registry,
    reset_engine,
)
from ferro.base import ForwardRelation, ManyToManyRelation, ReverseRelation


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


def test_relation_back_ref_helper_declares_reverse_relation():
    """Relation[list[T]] = Reverse(...) declares a reverse collection relation."""

    class Role(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        candidates: Relation[list["Candidate"]] = Reverse("role_id")

    class Candidate(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        role_id: Annotated[int, FerroField(foreign_key=Role)]
        role: Relation[Role] = Forward("role_id")

    candidates = Role.__ferro_relations__["candidates"]
    assert isinstance(candidates, ReverseRelation)
    assert candidates.attribute == "role_id"
    assert "candidates" not in Role.model_fields
    role = Role(name="Engineering")
    assert role.name == "Engineering"

    from ferro.relations import resolve_relationships

    resolve_relationships()
    assert hasattr(Role, "candidates")
    assert Role.candidates is not None


def test_relation_back_ref_field_equivalent_declares_reverse_relation():
    """Field(back_ref=True) is the lower-level equivalent of BackRef()."""

    class RoleViaField(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        candidates: Relation[list["CandidateViaField"]] = Field(back_ref=True)

    class CandidateViaField(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        role: Annotated[RoleViaField, ForeignKey(related_name="candidates")]

    assert RoleViaField.__ferro_relations__["candidates"] == "BackRef"
    assert "candidates" not in RoleViaField.model_fields

    from ferro.relations import resolve_relationships

    resolve_relationships()
    assert hasattr(RoleViaField, "candidates")


def test_relation_many_to_many_helper_declares_collection_relation():
    """Relation[list[T]] = ManyToMany(...) declares a many-to-many relation."""

    class Student(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        courses: Relation[list["Course"]] = ManyToMany(through="student_courses")

    class Course(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        students: Relation[list["Student"]] = Reverse("courses")

    rel = Student.__ferro_relations__["courses"]
    assert isinstance(rel, ManyToManyRelation)
    assert rel.through == "student_courses"
    assert Course.__ferro_relations__["students"].attribute == "courses"
    assert rel.to == "Course"
    assert "courses" not in Student.model_fields

    from ferro.relations import resolve_relationships

    resolve_relationships()
    assert hasattr(Student, "courses")
    assert hasattr(Course, "students")


def test_relation_many_to_many_field_equivalent_declares_collection_relation():
    """Field(many_to_many=True, ...) is the lower-level equivalent of ManyToMany()."""

    class StudentViaField(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        courses: Relation[list["CourseViaField"]] = Field(
            many_to_many=True, related_name="students", through="enrollments"
        )

    class CourseViaField(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        students: Relation[list["StudentViaField"]] = BackRef()

    rel = StudentViaField.__ferro_relations__["courses"]
    assert rel.related_name == "students"
    assert rel.through == "enrollments"
    assert rel.to == "CourseViaField"
    assert "courses" not in StudentViaField.model_fields


def test_old_backref_type_marker_raises_migration_error():
    """Old BackRef[...] type-marker syntax fails with actionable guidance."""

    with pytest.raises(TypeError, match="Relation\\[list\\[T\\]\\] = BackRef\\(\\)"):

        class OldRole(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            candidates: BackRef[list[int]] = None


def test_backref_plain_list_annotation_raises_migration_error():
    """BackRef() collection fields must use Relation[list[T]], not list[T]."""

    with pytest.raises(TypeError, match="Relation\\[list\\[T\\]\\] = BackRef\\(\\)"):

        class PlainListRole(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            candidates: list[int] = BackRef()


def test_many_to_many_field_import_removed_from_public_api():
    """ManyToManyField is no longer exported as public API."""
    import ferro

    assert not hasattr(ferro, "ManyToManyField")


def test_metadata_discovery():
    """Verify that the Metaclass finds the forward and reverse relations."""
    # Before resolution
    assert "author" in Post.__ferro_relations__
    assert "author_id" in Post.__ferro_query_columns__  # shadow FK column

    # After resolution
    from ferro.relations import resolve_relationships

    resolve_relationships()

    assert isinstance(Post.__ferro_relations__["author"], ForwardRelation)
    assert Post.__ferro_relations__["author"].fk_column == "author_id"

    # Verify shadow field generation
    assert "author_id" in Post.model_fields

    # Verify discovery on User
    assert "posts" in User.__ferro_relations__
    assert isinstance(User.__ferro_relations__["posts"], ReverseRelation)
    assert User.__ferro_relations__["posts"].attribute == "author_id"
    # Check if it's a descriptor
    assert hasattr(User, "posts")


def test_pydantic_isolation():
    """Verify that reverse relation fields are not required by Pydantic."""
    # Should NOT raise an error about 'posts' missing
    u = User(username="alice")
    assert u.username == "alice"


@pytest.mark.asyncio
async def test_forward_ref_resolution():
    """Verify that string/ForwardRef model references are resolved during connect()."""

    # 1. Define referencing model first
    class Post(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        author_id: Annotated[int, FerroField(foreign_key="Author")]
        author: Relation["Author"] = Forward("author_id")

    # 2. Define target model second
    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        posts: Relation[list[Post]] = Reverse("author_id")

    # Initially it's a string/ForwardRef
    raw_to = Post.__ferro_relations__["author"].to
    assert isinstance(raw_to, (str, ForwardRef))

    # 3. Simulate connect() / resolution
    from ferro.relations import resolve_relationships

    resolve_relationships()

    # Now it should be the class
    assert Post.__ferro_relations__["author"].to is Author
    assert Post.model_fields["author_id"].annotation is int


def test_relationship_validation_failure():
    """Verify that an error is raised if related_name doesn't match a field."""

    class User(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str
        # WRONG NAME HERE
        wrong_name: Relation[list["Post"]] = BackRef()

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
        posts: Relation[list["PostViaField"]] = Field(back_ref=True)

    class PostViaField(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author: Annotated[UserViaField, ForeignKey(related_name="posts")]

    assert "posts" in UserViaField.__ferro_relations__
    assert UserViaField.__ferro_relations__["posts"] == "BackRef"

    from ferro.relations import resolve_relationships

    resolve_relationships()
    assert hasattr(UserViaField, "posts")


def test_back_ref_via_annotated_field():
    """Annotated[list["Post"], Field(back_ref=True)] = None declares a reverse relation."""

    class UserAnnotated(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str
        posts: Annotated[Relation[list["PostAnnotated"]], Field(back_ref=True)]

    class PostAnnotated(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author: Annotated[UserAnnotated, ForeignKey(related_name="posts")]

    assert "posts" in UserAnnotated.__ferro_relations__
    assert UserAnnotated.__ferro_relations__["posts"] == "BackRef"

    from ferro.relations import resolve_relationships

    resolve_relationships()
    assert hasattr(UserAnnotated, "posts")


def test_back_ref_and_many_to_many_flags_raise():
    """Cannot mark one relation field as both reverse and many-to-many."""

    with pytest.raises(
        TypeError,
        match="cannot be both back_ref and many_to_many",
    ):

        class UserDouble(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            username: str
            posts: Relation[list["PostDouble"]] = Field(
                back_ref=True,
                many_to_many=True,
                related_name="users",
            )

        class PostDouble(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            author: Annotated[UserDouble, ForeignKey(related_name="posts")]
