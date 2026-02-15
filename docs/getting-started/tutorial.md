# Tutorial: Build a Blog API

In this tutorial, you'll build a simple blog API with Ferro in about 10 minutes. You'll learn how to:

- Define models with relationships
- Connect to a database
- Create, query, update, and delete records
- Work with one-to-many relationships

## Step 1: Install Ferro

First, install Ferro:

```bash
pip install ferro-orm
```

Create a new file called `blog.py`.

## Step 2: Define Your Models

Let's create a blog with users, posts, and comments:

```python
# blog.py
import asyncio
from datetime import datetime
from typing import Annotated
from ferro import Model, FerroField, ForeignKey, BackRef, connect

class User(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    username: Annotated[str, FerroField(unique=True)]
    email: Annotated[str, FerroField(unique=True)]
    posts: BackRef[list["Post"]] = None
    comments: BackRef[list["Comment"]] = None

class Post(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    title: str
    content: str
    published: bool = False
    created_at: datetime = datetime.now()
    author: Annotated[User, ForeignKey(related_name="posts")]
    comments: BackRef[list["Comment"]] = None

class Comment(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    text: str
    created_at: datetime = datetime.now()
    author: Annotated[User, ForeignKey(related_name="comments")]
    post: Annotated[Post, ForeignKey(related_name="comments")]

async def main():
    # We'll add code here
    pass

if __name__ == "__main__":
    asyncio.run(main())
```

**What you just did:**

- Created three models: `User`, `Post`, and `Comment`
- Defined relationships: Users have posts and comments, posts have comments
- Used `BackRef` for the reverse side of relationships
- Set primary keys and unique constraints

## Step 3: Connect to the Database

Add the connection code to `main()`:

```python
async def main():
    # Connect to SQLite with auto-migration
    await connect("sqlite:blog.db?mode=rwc", auto_migrate=True)
    print("✅ Connected to database")
```

Run it:

```bash
python blog.py
```

Output:
```
✅ Connected to database
```

**What happened:**

- Ferro connected to a SQLite database (creates `blog.db` if it doesn't exist)
- `auto_migrate=True` automatically created all tables based on your models
- The Rust engine generated `CREATE TABLE` statements for all three models

## Step 4: Create Some Data

Let's add users, posts, and comments:

```python
async def main():
    await connect("sqlite:blog.db?mode=rwc", auto_migrate=True)

    # Create users
    alice = await User.create(
        username="alice",
        email="alice@example.com"
    )
    bob = await User.create(
        username="bob",
        email="bob@example.com"
    )
    print(f"✅ Created users: {alice.username}, {bob.username}")

    # Create posts
    post1 = await Post.create(
        title="Why Ferro is Fast",
        content="Ferro uses a Rust engine for SQL generation...",
        published=True,
        author=alice
    )
    post2 = await Post.create(
        title="Getting Started with Async Python",
        content="Async programming can be tricky...",
        published=True,
        author=alice
    )
    draft = await Post.create(
        title="Draft Post",
        content="This is not published yet",
        published=False,
        author=bob
    )
    print(f"✅ Created {await Post.select().count()} posts")

    # Create comments
    comment1 = await Comment.create(
        text="Great article!",
        author=bob,
        post=post1
    )
    comment2 = await Comment.create(
        text="Thanks for sharing",
        author=alice,
        post=post1
    )
    print(f"✅ Created {await Comment.select().count()} comments")
```

Run it again:

```bash
python blog.py
```

Output:
```
✅ Connected to database
✅ Created users: alice, bob
✅ Created 3 posts
✅ Created 2 comments
```

**What you learned:**

- `.create()` inserts a record and returns the model instance
- Foreign keys accept model instances (e.g., `author=alice`)
- `.count()` returns the total number of records

## Step 5: Query Your Data

Add query examples:

```python
async def main():
    await connect("sqlite:blog.db?mode=rwc", auto_migrate=True)

    # ... (previous create code) ...

    # Query: Find all published posts
    published = await Post.where(Post.published == True).all()
    print(f"\n📚 Found {len(published)} published posts:")
    for post in published:
        print(f"  - {post.title}")

    # Query: Find posts by a specific author
    alice = await User.where(User.username == "alice").first()
    alice_posts = await Post.where(Post.author_id == alice.id).all()
    print(f"\n✍️  Alice wrote {len(alice_posts)} posts")

    # Query: Get a post with its author
    post = await Post.where(Post.title.like("%Fast%")).first()
    if post:
        author = await post.author
        print(f"\n📝 Post: '{post.title}' by {author.username}")

    # Query: Get comments for a post
    post_comments = await post.comments.all()
    print(f"💬 This post has {len(post_comments)} comments:")
    for comment in post_comments:
        comment_author = await comment.author
        print(f"  - {comment_author.username}: {comment.text}")
```

Run it:

```bash
python blog.py
```

Output:
```
✅ Connected to database
✅ Created users: alice, bob
✅ Created 3 posts
✅ Created 2 comments

📚 Found 2 published posts:
  - Why Ferro is Fast
  - Getting Started with Async Python

✍️  Alice wrote 2 posts

📝 Post: 'Why Ferro is Fast' by alice
💬 This post has 2 comments:
  - bob: Great article!
  - alice: Thanks for sharing
```

**What you learned:**

- `.where()` filters records with Python comparison operators
- `.all()` returns a list, `.first()` returns one or None
- `.like()` for pattern matching
- Access forward relationships with `await post.author`
- Access reverse relationships with `await post.comments.all()`

## Step 6: Update Records

Add update examples:

```python
async def main():
    await connect("sqlite:blog.db?mode=rwc", auto_migrate=True)

    # ... (previous code) ...

    # Update: Publish Bob's draft
    draft = await Post.where(
        (Post.author_id == bob.id) & (Post.published == False)
    ).first()

    if draft:
        draft.published = True
        await draft.save()
        print(f"\n✅ Published draft: {draft.title}")

    # Batch update: Mark all posts as needing review
    updated = await Post.where(Post.published == True).update(
        title=Post.title + " [REVIEWED]"
    )
    print(f"✅ Updated {updated} posts")
```

**What you learned:**

- Update individual records with `.save()`
- Batch update with `.update()` (more efficient for multiple records)
- Combine filters with `&` (AND) and `|` (OR)

## Step 7: Delete Records

Add delete examples:

```python
async def main():
    await connect("sqlite:blog.db?mode=rwc", auto_migrate=True)

    # ... (previous code) ...

    # Delete: Remove a specific comment
    spam_comment = await Comment.where(Comment.text.like("%spam%")).first()
    if spam_comment:
        await spam_comment.delete()
        print(f"\n🗑️  Deleted spam comment")

    # Batch delete: Remove all unpublished posts
    deleted = await Post.where(Post.published == False).delete()
    print(f"🗑️  Deleted {deleted} unpublished posts")
```

**What you learned:**

- `.delete()` on an instance removes that record
- `.delete()` on a query removes all matching records
- Ferro handles cascade deletes based on foreign key constraints

## Complete Code

Here's the full tutorial code:

```python
# blog.py
import asyncio
from datetime import datetime
from typing import Annotated
from ferro import Model, FerroField, ForeignKey, BackRef, connect

class User(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    username: Annotated[str, FerroField(unique=True)]
    email: Annotated[str, FerroField(unique=True)]
    posts: BackRef[list["Post"]] = None
    comments: BackRef[list["Comment"]] = None

class Post(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    title: str
    content: str
    published: bool = False
    created_at: datetime = datetime.now()
    author: Annotated[User, ForeignKey(related_name="posts")]
    comments: BackRef[list["Comment"]] = None

class Comment(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    text: str
    created_at: datetime = datetime.now()
    author: Annotated[User, ForeignKey(related_name="comments")]
    post: Annotated[Post, ForeignKey(related_name="comments")]

async def main():
    # Connect
    await connect("sqlite:blog.db?mode=rwc", auto_migrate=True)

    # Create
    alice = await User.create(username="alice", email="alice@example.com")
    bob = await User.create(username="bob", email="bob@example.com")

    post1 = await Post.create(
        title="Why Ferro is Fast",
        content="Ferro uses a Rust engine...",
        published=True,
        author=alice
    )

    await Comment.create(text="Great article!", author=bob, post=post1)

    # Query
    published = await Post.where(Post.published == True).all()
    print(f"Found {len(published)} published posts")

    # Relationships
    post_author = await post1.author
    print(f"Post by: {post_author.username}")

    author_posts = await alice.posts.all()
    print(f"Alice has {len(author_posts)} posts")

if __name__ == "__main__":
    asyncio.run(main())
```

## What You Learned

In this tutorial, you learned:

✅ How to define models with `Model` and type hints
✅ How to add constraints with `FerroField` or `Field`
✅ How to create relationships with `ForeignKey` and `BackRef`
✅ How to connect to a database with `connect()`
✅ How to create records with `.create()`
✅ How to query with `.where()`, `.all()`, `.first()`
✅ How to update with `.save()` and `.update()`
✅ How to delete with `.delete()`
✅ How to access relationships with `await`

## Next Steps

Now that you understand the basics:

- **[User Guide](../guide/models-and-fields.md)** — Deep dive into models, fields, and relationships
- **[Queries](../guide/queries.md)** — Learn advanced filtering, ordering, and aggregation
- **[How-To: Testing](../howto/testing.md)** — Set up a test suite for your Ferro app
- **[Migrations](../guide/migrations.md)** — Use Alembic for production schema management

Happy coding! 🎉
