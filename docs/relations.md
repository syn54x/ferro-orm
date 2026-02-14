# Relations

Ferro provides a robust system for connecting models, supporting standard relational patterns with zero-boilerplate reverse lookups and automated join table management.

## One-to-Many

The most common relationship type. It is defined using a `ForeignKey` on the "child" model and a `BackRef` marker (or `Field(back_ref=True)`) on the "parent" model.

```python
from typing import Annotated
from ferro import Model, ForeignKey, BackRef

class Author(Model):
    id: int
    name: str
    # Marker for reverse lookup; provides full Query intellisense
    posts: BackRef[list["Post"]] = None

class Post(Model):
    id: int
    title: str
    # Defines the forward link and the name of the reverse field
    author: Annotated[Author, ForeignKey(related_name="posts")]
```

You can also declare the reverse relation with `Field(back_ref=True)` so the annotation stays a plain type:

```python
from ferro import Model, ForeignKey, Field

class Author(Model):
    id: int
    name: str
    posts: list["Post"] | None = Field(default=None, back_ref=True)

class Post(Model):
    id: int
    title: str
    author: Annotated[Author, ForeignKey(related_name="posts")]
```

Or with `Annotated`: `posts: Annotated[list["Post"] | None, Field(back_ref=True)] = None`. Do not use both `BackRef` and `back_ref=True` on the same field.

### Shadow Fields
For every `ForeignKey` field (e.g., `author`), Ferro automatically creates a "shadow" ID column in the database (e.g., `author_id`). You can access or filter by this field directly via `post.author_id`.

## One-to-One

A strict 1:1 link is created by adding `unique=True` to a `ForeignKey`.

```python
class Profile(Model):
    user: Annotated[User, ForeignKey(related_name="profile", unique=True)]
```

**Behavioral Difference:**

- **Forward**: Accessing `await profile.user` returns a single `User` object.
- **Reverse**: Accessing `await user.profile` returns a single `Profile` object (internally calls `.first()`) instead of a `Query` object.

## Many-to-Many

Defined using the `ManyToManyField`. Ferro automatically manages the hidden join table required for this relationship.

```python
from ferro import ManyToManyField

class Student(Model):
    name: str
    courses: Annotated[list["Course"], ManyToManyField(related_name="students")] = None

class Course(Model):
    title: str
    students: list["Student"] | None = Field(default=None, back_ref=True)
```

### Join Table Management
The Rust engine automatically registers and creates a join table (e.g., `student_courses`) when the models are initialized. You do not need to define a "through" model manually unless you need custom fields on the link.

### Relationship Mutators
Many-to-Many relationships provide specialized methods for managing links:

- **`.add(*instances)`**: Create new links in the join table.
- **`.remove(*instances)`**: Remove specific links.
- **`.clear()`**: Remove all links for the current instance.

```python
await student.courses.add(math_101, physics_202)
await student.courses.clear()
```

## Lazy Loading vs. Queries

Ferro relations are **lazy**. Data is never fetched until you explicitly request it.

1.  **Forward Relations**: Accessing a `ForeignKey` returns an awaitable descriptor.
    ```python
    author = await post.author  # Database hit
    ```
2.  **Reverse/M2M Relations**: Accessing a `BackRef` (or a field declared with `back_ref=True`) or `ManyToManyField` returns a `Query` object. This allows you to chain further filters before execution.
    ```python
    posts = await author.posts.where(Post.published == True).all()
    ```
