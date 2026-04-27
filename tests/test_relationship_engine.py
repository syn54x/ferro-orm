from typing import Annotated, ForwardRef

import pytest

from ferro import (
    BackRef,
    FerroField,
    Field,
    ForeignKey,
    ManyToMany,
    Model,
    Relation,
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
    posts: Relation[list["Post"]] = BackRef()


class Post(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: str
    # Forward link
    author: Annotated[User, ForeignKey(related_name="posts")]


def test_relation_back_ref_helper_declares_reverse_relation():
    """Relation[list[T]] = BackRef() declares a reverse collection relation."""

    class Role(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        candidates: Relation[list["Candidate"]] = BackRef()

    class Candidate(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        role: Annotated[Role, ForeignKey(related_name="candidates")]

    assert Role.ferro_relations["candidates"] == "BackRef"
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

    assert RoleViaField.ferro_relations["candidates"] == "BackRef"
    assert "candidates" not in RoleViaField.model_fields

    from ferro.relations import resolve_relationships

    resolve_relationships()
    assert hasattr(RoleViaField, "candidates")


def test_relation_many_to_many_helper_declares_collection_relation():
    """Relation[list[T]] = ManyToMany(...) declares a many-to-many relation."""

    class Student(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        courses: Relation[list["Course"]] = ManyToMany(related_name="students")

    class Course(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        students: Relation[list["Student"]] = BackRef()

    rel = Student.ferro_relations["courses"]
    assert rel.related_name == "students"
    assert rel.through is None
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

    rel = StudentViaField.ferro_relations["courses"]
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
        posts: Relation[list[Post]] = BackRef()

    # Initially it's a string/ForwardRef
    raw_to = Post.ferro_relations["author"].to
    assert isinstance(raw_to, (str, ForwardRef))

    # 3. Simulate connect() / resolution
    from ferro.relations import resolve_relationships

    resolve_relationships()

    # Now it should be the class
    assert Post.ferro_relations["author"].to is Author
    assert Post.__annotations__["author_id"] == (int | None)
    assert Post.model_fields["author_id"].annotation == (int | None)


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
        posts: Annotated[Relation[list["PostAnnotated"]], Field(back_ref=True)]

    class PostAnnotated(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author: Annotated[UserAnnotated, ForeignKey(related_name="posts")]

    assert "posts" in UserAnnotated.ferro_relations
    assert UserAnnotated.ferro_relations["posts"] == "BackRef"

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
