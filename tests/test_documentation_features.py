"""
Comprehensive integration tests for documented Ferro features.

This test suite validates that all features documented in the user guide
work as expected. Each test corresponds to a specific documented capability.
"""

import asyncio
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated

import pytest

from ferro import (
    BackRef,
    FerroField,
    Field,
    ForeignKey,
    ManyToManyField,
    Model,
    connect,
    create_tables,
    transaction,
)


# Test Models
class UserRole(Enum):
    """Test enum for role field"""

    USER = "user"
    ADMIN = "admin"
    MODERATOR = "moderator"


class User(Model):
    """User model for testing"""

    id: Annotated[int | None, FerroField(primary_key=True)] = None
    username: Annotated[str, FerroField(unique=True)]
    email: Annotated[str, FerroField(unique=True, index=True)]
    is_active: bool = True
    role: UserRole = UserRole.USER
    posts: BackRef[list["Post"]] = None
    comments: BackRef[list["Comment"]] = None


class Post(Model):
    """Post model for testing"""

    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: str
    content: str
    published: bool = False
    created_at: datetime = Field(default_factory=datetime.now)
    author: Annotated[User, ForeignKey(related_name="posts")]
    comments: BackRef[list["Comment"]] = None
    tags: Annotated[list["Tag"], ManyToManyField(related_name="posts")] = None


class Comment(Model):
    """Comment model for testing"""

    id: Annotated[int | None, FerroField(primary_key=True)] = None
    text: str
    created_at: datetime = Field(default_factory=datetime.now)
    author: Annotated[User, ForeignKey(related_name="comments")]
    post: Annotated[Post, ForeignKey(related_name="comments")]


class Tag(Model):
    """Tag model for testing many-to-many"""

    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: Annotated[str, FerroField(unique=True)]
    posts: BackRef[list["Post"]] = None


class Product(Model):
    """Product model for testing field types"""

    sku: str = Field(primary_key=True)
    name: str
    price: Decimal = Field(ge=0, decimal_places=2)
    stock: int = 0
    created_date: date = Field(default_factory=date.today)
    metadata_json: dict | None = None


# Re-register models before each test so they are in the Rust and Python
# registries even if another test's fixture cleared them (e.g. alembic/schema).
@pytest.fixture(autouse=True)
def _ensure_models_registered():
    from ferro.state import _MODEL_REGISTRY_PY

    for model_cls in (User, Post, Comment, Tag, Product):
        model_cls._reregister_ferro()
        _MODEL_REGISTRY_PY[model_cls.__name__] = model_cls
    yield


@pytest.fixture
def db_url():
    """Generate a unique database URL for each test"""
    import uuid

    db_file = f"test_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    # Cleanup
    import os

    if os.path.exists(db_file):
        os.remove(db_file)


# ============================================================================
# MODELS & FIELDS TESTS (docs/guide/models-and-fields.md)
# ============================================================================


@pytest.mark.asyncio
async def test_basic_model_definition(db_url):
    """Test basic model definition from docs"""
    await connect(db_url, auto_migrate=True)

    class SimpleUser(Model):
        id: int
        username: str
        is_active: bool = True

    await connect(db_url, auto_migrate=True)
    await create_tables()
    user = SimpleUser(id=1, username="alice")
    assert user.username == "alice"
    assert user.is_active is True


@pytest.mark.asyncio
async def test_field_types(db_url):
    """Test all documented field types work correctly"""
    await connect(db_url, auto_migrate=True)
    product = await Product.create(
        sku="PROD-001",
        name="Test Product",
        price=Decimal("19.99"),
        stock=100,
        metadata_json={"color": "blue", "size": "large"},
    )

    assert product.sku == "PROD-001"
    assert product.price == Decimal("19.99")
    assert product.stock == 100
    assert product.metadata_json["color"] == "blue"
    assert isinstance(product.created_date, date)


@pytest.mark.asyncio
async def test_enum_field_type(db_url):
    """Test enum field type works as documented"""
    await connect(db_url, auto_migrate=True)
    user = await User.create(
        username="admin_user", email="admin@example.com", role=UserRole.ADMIN
    )

    fetched = await User.get(user.id)
    assert fetched.role == UserRole.ADMIN
    assert isinstance(fetched.role, UserRole)


@pytest.mark.asyncio
async def test_field_constraints_pydantic_style(db_url):
    """Test Field() constraint syntax"""
    await connect(db_url, auto_migrate=True)
    product = await Product.create(sku="TEST-001", name="Test", price=Decimal("10.00"))
    assert product.sku == "TEST-001"


@pytest.mark.asyncio
async def test_field_constraints_annotated_style(db_url):
    """Test FerroField() annotated syntax"""
    await connect(db_url, auto_migrate=True)
    user = await User.create(username="test", email="test@example.com")
    assert user.username == "test"

    # Verify unique constraint
    with pytest.raises(Exception):  # Should raise integrity error
        await User.create(username="test", email="other@example.com")


# ============================================================================
# CRUD OPERATIONS TESTS (docs/guide/mutations.md)
# ============================================================================


@pytest.mark.asyncio
async def test_create_method(db_url):
    """Test Model.create() as documented"""
    await connect(db_url, auto_migrate=True)
    user = await User.create(
        username="alice", email="alice@example.com", is_active=True
    )

    assert user.id is not None
    assert user.username == "alice"
    assert user.email == "alice@example.com"


@pytest.mark.asyncio
async def test_get_method(db_url):
    """Test Model.get() as documented"""
    await connect(db_url, auto_migrate=True)
    user = await User.create(username="bob", email="bob@example.com")

    fetched = await User.get(user.id)
    assert fetched is not None
    assert fetched.id == user.id
    assert fetched.username == "bob"


@pytest.mark.asyncio
async def test_all_method(db_url):
    """Test Model.all() as documented"""
    await connect(db_url, auto_migrate=True)
    await User.create(username="user1", email="user1@example.com")
    await User.create(username="user2", email="user2@example.com")

    all_users = await User.all()
    assert len(all_users) == 2


@pytest.mark.asyncio
async def test_save_method(db_url):
    """Test instance.save() as documented"""
    await connect(db_url, auto_migrate=True)
    user = await User.create(username="alice", email="alice@example.com")

    user.email = "alice.new@example.com"
    user.is_active = False
    await user.save()

    fetched = await User.get(user.id)
    assert fetched.email == "alice.new@example.com"
    assert fetched.is_active is False


@pytest.mark.asyncio
async def test_delete_method(db_url):
    """Test instance.delete() as documented"""
    await connect(db_url, auto_migrate=True)
    user = await User.create(username="alice", email="alice@example.com")
    user_id = user.id

    await user.delete()

    fetched = await User.get(user_id)
    assert fetched is None


@pytest.mark.asyncio
async def test_refresh_method(db_url):
    """Test instance.refresh() as documented"""
    await connect(db_url, auto_migrate=True)
    user = await User.create(username="alice", email="alice@example.com")

    # Simulate external update
    await User.where(User.id == user.id).update(email="updated@example.com")

    # Refresh instance
    await user.refresh()
    assert user.email == "updated@example.com"


@pytest.mark.asyncio
async def test_bulk_create(db_url):
    """Test Model.bulk_create() as documented"""
    await connect(db_url, auto_migrate=True)
    users = [
        User(username=f"user_{i}", email=f"user{i}@example.com") for i in range(100)
    ]

    count = await User.bulk_create(users)
    assert count == 100

    all_users = await User.all()
    assert len(all_users) == 100


@pytest.mark.asyncio
async def test_get_or_create(db_url):
    """Test Model.get_or_create() as documented"""
    await connect(db_url, auto_migrate=True)
    # First call creates
    user1, created1 = await User.get_or_create(
        email="test@example.com", defaults={"username": "testuser"}
    )
    assert created1 is True
    assert user1.username == "testuser"

    # Second call retrieves
    user2, created2 = await User.get_or_create(
        email="test@example.com", defaults={"username": "different"}
    )
    assert created2 is False
    assert user2.id == user1.id
    assert user2.username == "testuser"  # Defaults not applied


@pytest.mark.asyncio
async def test_update_or_create(db_url):
    """Test Model.update_or_create() as documented"""
    await connect(db_url, auto_migrate=True)
    # First call creates
    user1, created1 = await User.update_or_create(
        email="test@example.com", defaults={"username": "testuser"}
    )
    assert created1 is True

    # Second call updates
    user2, created2 = await User.update_or_create(
        email="test@example.com", defaults={"username": "updated"}
    )
    assert created2 is False
    assert user2.id == user1.id
    assert user2.username == "updated"


# ============================================================================
# QUERY OPERATIONS TESTS (docs/guide/queries.md)
# ============================================================================


@pytest.mark.asyncio
async def test_where_equality(db_url):
    """Test .where() with equality operator"""
    await connect(db_url, auto_migrate=True)
    await User.create(username="alice", email="alice@example.com", is_active=True)
    await User.create(username="bob", email="bob@example.com", is_active=False)

    active_users = await User.where(User.is_active == True).all()
    assert len(active_users) == 1
    assert active_users[0].username == "alice"


@pytest.mark.asyncio
async def test_where_comparison_operators(db_url):
    """Test comparison operators in queries"""
    await connect(db_url, auto_migrate=True)
    for i in range(5):
        await Product.create(
            sku=f"PROD-{i}", name=f"Product {i}", price=Decimal(str(i * 10))
        )

    # Greater than
    expensive = await Product.where(Product.price > Decimal("20")).all()
    assert len(expensive) == 2  # 30 and 40

    # Less than or equal
    cheap = await Product.where(Product.price <= Decimal("20")).all()
    assert len(cheap) == 3  # 0, 10, 20


@pytest.mark.asyncio
async def test_where_like_operator(db_url):
    """Test .like() operator as documented"""
    await connect(db_url, auto_migrate=True)
    await User.create(username="alice", email="alice@gmail.com")
    await User.create(username="bob", email="bob@yahoo.com")
    await User.create(username="charlie", email="charlie@gmail.com")

    gmail_users = await User.where(User.email.like("%gmail.com")).all()
    assert len(gmail_users) == 2


@pytest.mark.asyncio
async def test_where_in_operator(db_url):
    """Test .in_() operator as documented"""
    await connect(db_url, auto_migrate=True)
    await User.create(username="alice", email="alice@example.com", role=UserRole.ADMIN)
    await User.create(username="bob", email="bob@example.com", role=UserRole.MODERATOR)
    await User.create(
        username="charlie", email="charlie@example.com", role=UserRole.USER
    )

    # Use enum values instead of enum instances
    staff = await User.where(
        User.role.in_([UserRole.ADMIN.value, UserRole.MODERATOR.value])
    ).all()
    assert len(staff) == 2


@pytest.mark.asyncio
async def test_logical_and_operator(db_url):
    """Test & (AND) operator in queries"""
    await connect(db_url, auto_migrate=True)
    await User.create(
        username="alice", email="alice@example.com", is_active=True, role=UserRole.ADMIN
    )
    await User.create(
        username="bob", email="bob@example.com", is_active=True, role=UserRole.USER
    )
    await User.create(
        username="charlie",
        email="charlie@example.com",
        is_active=False,
        role=UserRole.ADMIN,
    )

    active_admins = await User.where(
        (User.is_active == True) & (User.role == UserRole.ADMIN.value)
    ).all()
    assert len(active_admins) == 1
    assert active_admins[0].username == "alice"


@pytest.mark.asyncio
async def test_logical_or_operator(db_url):
    """Test | (OR) operator in queries"""
    await connect(db_url, auto_migrate=True)
    await User.create(username="alice", email="alice@example.com", role=UserRole.ADMIN)
    await User.create(username="bob", email="bob@example.com", role=UserRole.MODERATOR)
    await User.create(
        username="charlie", email="charlie@example.com", role=UserRole.USER
    )

    staff = await User.where(
        (User.role == UserRole.ADMIN.value) | (User.role == UserRole.MODERATOR.value)
    ).all()
    assert len(staff) == 2


@pytest.mark.asyncio
async def test_order_by(db_url):
    """Test .order_by() as documented"""
    await connect(db_url, auto_migrate=True)
    await User.create(username="charlie", email="charlie@example.com")
    await User.create(username="alice", email="alice@example.com")
    await User.create(username="bob", email="bob@example.com")

    # Ascending
    users_asc = await User.select().order_by(User.username, "asc").all()
    assert users_asc[0].username == "alice"
    assert users_asc[1].username == "bob"
    assert users_asc[2].username == "charlie"

    # Descending
    users_desc = await User.select().order_by(User.username, "desc").all()
    assert users_desc[0].username == "charlie"


@pytest.mark.asyncio
async def test_limit_and_offset(db_url):
    """Test .limit() and .offset() as documented"""
    await connect(db_url, auto_migrate=True)
    for i in range(10):
        await User.create(username=f"user_{i}", email=f"user{i}@example.com")

    # Limit
    first_5 = await User.select().order_by(User.id).limit(5).all()
    assert len(first_5) == 5

    # Offset
    skip_5 = await User.select().order_by(User.id).offset(5).limit(5).all()
    assert len(skip_5) == 5
    assert skip_5[0].id != first_5[0].id


@pytest.mark.asyncio
async def test_query_first(db_url):
    """Test .first() as documented"""
    await connect(db_url, auto_migrate=True)
    await User.create(username="alice", email="alice@example.com")

    user = await User.where(User.username == "alice").first()
    assert user is not None
    assert user.username == "alice"

    none_user = await User.where(User.username == "nonexistent").first()
    assert none_user is None


@pytest.mark.asyncio
async def test_query_count(db_url):
    """Test .count() as documented"""
    await connect(db_url, auto_migrate=True)
    await User.create(username="alice", email="alice@example.com", is_active=True)
    await User.create(username="bob", email="bob@example.com", is_active=True)
    await User.create(username="charlie", email="charlie@example.com", is_active=False)

    active_count = await User.where(User.is_active == True).count()
    assert active_count == 2


@pytest.mark.asyncio
async def test_query_exists(db_url):
    """Test .exists() as documented"""
    await connect(db_url, auto_migrate=True)
    await User.create(username="alice", email="alice@example.com", role=UserRole.ADMIN)

    has_admin = await User.where(User.role == UserRole.ADMIN.value).exists()
    assert has_admin is True

    has_moderator = await User.where(User.role == UserRole.MODERATOR.value).exists()
    assert has_moderator is False


@pytest.mark.asyncio
async def test_query_update(db_url):
    """Test .update() as documented"""
    await connect(db_url, auto_migrate=True)
    await User.create(username="alice", email="alice@example.com", is_active=True)
    await User.create(username="bob", email="bob@example.com", is_active=True)

    count = await User.where(User.is_active == True).update(is_active=False)
    assert count == 2

    active_users = await User.where(User.is_active == True).all()
    assert len(active_users) == 0


@pytest.mark.asyncio
async def test_query_delete(db_url):
    """Test .delete() on query as documented"""
    await connect(db_url, auto_migrate=True)
    await User.create(username="alice", email="alice@example.com", is_active=True)
    await User.create(username="bob", email="bob@example.com", is_active=False)

    count = await User.where(User.is_active == False).delete()
    assert count == 1

    remaining = await User.all()
    assert len(remaining) == 1
    assert remaining[0].username == "alice"


# ============================================================================
# RELATIONSHIPS TESTS (docs/guide/relationships.md)
# ============================================================================


@pytest.mark.asyncio
async def test_foreign_key_creation(db_url):
    """Test creating records with ForeignKey relationships"""
    await connect(db_url, auto_migrate=True)
    author = await User.create(username="alice", email="alice@example.com")

    # Pass model instance
    post = await Post.create(title="My Post", content="Content here", author=author)

    assert post.author_id == author.id


@pytest.mark.asyncio
async def test_foreign_key_forward_relation(db_url):
    """Test accessing forward relation (ForeignKey)"""
    await connect(db_url, auto_migrate=True)
    author = await User.create(username="alice", email="alice@example.com")
    post = await Post.create(title="Test", content="Content", author=author)

    # Access forward relation
    post_author = await post.author
    assert post_author.id == author.id
    assert post_author.username == "alice"


@pytest.mark.asyncio
async def test_foreign_key_reverse_relation(db_url):
    """Test accessing reverse relation (BackRef)"""
    await connect(db_url, auto_migrate=True)
    author = await User.create(username="alice", email="alice@example.com")

    await Post.create(title="Post 1", content="Content 1", author=author)
    await Post.create(title="Post 2", content="Content 2", author=author)

    # Access reverse relation
    author_posts = await author.posts.all()
    assert len(author_posts) == 2


@pytest.mark.asyncio
async def test_reverse_relation_filtering(db_url):
    """Test filtering on reverse relations"""
    await connect(db_url, auto_migrate=True)
    author = await User.create(username="alice", email="alice@example.com")

    await Post.create(
        title="Published", content="Content", author=author, published=True
    )
    await Post.create(title="Draft", content="Content", author=author, published=False)

    published = await author.posts.where(Post.published == True).all()
    assert len(published) == 1
    assert published[0].title == "Published"


@pytest.mark.asyncio
async def test_shadow_field_access(db_url):
    """Test accessing shadow fields (author_id) as documented"""
    await connect(db_url, auto_migrate=True)
    author = await User.create(username="alice", email="alice@example.com")
    post = await Post.create(title="Test", content="Content", author=author)

    # Access shadow field
    assert post.author_id == author.id

    # Query by shadow field
    posts = await Post.where(Post.author_id == author.id).all()
    assert len(posts) == 1


@pytest.mark.skip(
    reason="Many-to-many join tables not automatically created - see coming-soon.md"
)
@pytest.mark.asyncio
async def test_many_to_many_add(db_url):
    """Test .add() for many-to-many relationships"""
    await connect(db_url, auto_migrate=True)
    post = await Post.create(
        title="Test Post",
        content="Content",
        author=await User.create(username="alice", email="alice@example.com"),
    )

    tag1 = await Tag.create(name="python")
    tag2 = await Tag.create(name="rust")

    await post.tags.add(tag1, tag2)

    post_tags = await post.tags.all()
    assert len(post_tags) == 2


@pytest.mark.skip(
    reason="Many-to-many join tables not automatically created - see coming-soon.md"
)
@pytest.mark.asyncio
async def test_many_to_many_remove(db_url):
    """Test .remove() for many-to-many relationships"""
    await connect(db_url, auto_migrate=True)
    post = await Post.create(
        title="Test Post",
        content="Content",
        author=await User.create(username="alice", email="alice@example.com"),
    )

    tag1 = await Tag.create(name="python")
    tag2 = await Tag.create(name="rust")

    await post.tags.add(tag1, tag2)
    await post.tags.remove(tag1)

    post_tags = await post.tags.all()
    assert len(post_tags) == 1
    assert post_tags[0].name == "rust"


@pytest.mark.skip(
    reason="Many-to-many join tables not automatically created - see coming-soon.md"
)
@pytest.mark.asyncio
async def test_many_to_many_clear(db_url):
    """Test .clear() for many-to-many relationships"""
    await connect(db_url, auto_migrate=True)
    post = await Post.create(
        title="Test Post",
        content="Content",
        author=await User.create(username="alice", email="alice@example.com"),
    )

    tag1 = await Tag.create(name="python")
    tag2 = await Tag.create(name="rust")

    await post.tags.add(tag1, tag2)
    await post.tags.clear()

    post_tags = await post.tags.all()
    assert len(post_tags) == 0


@pytest.mark.skip(
    reason="Many-to-many join tables not automatically created - see coming-soon.md"
)
@pytest.mark.asyncio
async def test_many_to_many_reverse(db_url):
    """Test many-to-many from both sides"""
    await connect(db_url, auto_migrate=True)
    post = await Post.create(
        title="Test Post",
        content="Content",
        author=await User.create(username="alice", email="alice@example.com"),
    )
    tag = await Tag.create(name="python")

    await post.tags.add(tag)

    # Access from reverse side
    tag_posts = await tag.posts.all()
    assert len(tag_posts) == 1
    assert tag_posts[0].id == post.id


# ============================================================================
# TRANSACTIONS TESTS (docs/guide/transactions.md)
# ============================================================================


@pytest.mark.asyncio
async def test_transaction_commit(db_url):
    """Test transaction commits on success"""
    await connect(db_url, auto_migrate=True)
    async with transaction():
        user = await User.create(username="alice", email="alice@example.com")
        await Post.create(title="Test", content="Content", author=user)

    # Verify data persisted
    users = await User.all()
    posts = await Post.all()
    assert len(users) == 1
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_transaction_rollback(db_url):
    """Test transaction rolls back on exception"""
    await connect(db_url, auto_migrate=True)
    try:
        async with transaction():
            await User.create(username="alice", email="alice@example.com")
            raise ValueError("Test error")
    except ValueError:
        pass

    # Verify data was rolled back
    users = await User.all()
    assert len(users) == 0


@pytest.mark.asyncio
async def test_transaction_isolation(db_url):
    """Test transaction isolation between concurrent tasks"""
    await connect(db_url, auto_migrate=True)

    async def task_a():
        async with transaction():
            await User.create(username="task_a_user", email="a@example.com")
            await asyncio.sleep(0.1)

    async def task_b():
        async with transaction():
            await User.create(username="task_b_user", email="b@example.com")

    await asyncio.gather(task_a(), task_b())

    users = await User.all()
    assert len(users) == 2


# ============================================================================
# TUTORIAL EXAMPLES TESTS (docs/getting-started/tutorial.md)
# ============================================================================


@pytest.mark.asyncio
async def test_tutorial_blog_example(db_url):
    """Test the complete tutorial blog example"""
    await connect(db_url, auto_migrate=True)
    # Create users
    alice = await User.create(username="alice", email="alice@example.com")
    bob = await User.create(username="bob", email="bob@example.com")

    # Create posts
    post1 = await Post.create(
        title="Why Ferro is Fast",
        content="Ferro uses a Rust engine...",
        published=True,
        author=alice,
    )

    post2 = await Post.create(
        title="Getting Started with Async Python",
        content="Async programming can be tricky...",
        published=True,
        author=alice,
    )

    draft = await Post.create(
        title="Draft Post",
        content="This is not published yet",
        published=False,
        author=bob,
    )

    # Create comments
    comment1 = await Comment.create(text="Great article!", author=bob, post=post1)

    comment2 = await Comment.create(text="Thanks for sharing", author=alice, post=post1)

    # Query: Find all published posts
    published = await Post.where(Post.published == True).all()
    assert len(published) == 2

    # Query: Find posts by author
    alice_posts = await Post.where(Post.author_id == alice.id).all()
    assert len(alice_posts) == 2

    # Query: Get post with pattern matching
    post = await Post.where(Post.title.like("%Fast%")).first()
    assert post is not None
    assert post.title == "Why Ferro is Fast"

    # Query: Access forward relation
    post_author = await post.author
    assert post_author.username == "alice"

    # Query: Access reverse relation
    post_comments = await post.comments.all()
    assert len(post_comments) == 2

    # Update: Publish draft
    draft.published = True
    await draft.save()

    published_after = await Post.where(Post.published == True).all()
    assert len(published_after) == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
