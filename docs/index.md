# Ferro ORM

**The async Python ORM with Rust speed and Pydantic ergonomics.**

<div class="grid cards" markdown>

-   :zap:{ .lg .middle } **Rust-Powered**

    ---

    All SQL generation and row hydration handled by a high-performance Rust engine. Minimize the "Python tax" on data-heavy operations.

-   :snake:{ .lg .middle } **Pydantic-Native**

    ---

    Leverage Pydantic V2 for schema definition and validation. Full IDE support, type safety, and familiar syntax.

-   :rocket:{ .lg .middle } **Async-First**

    ---

    Built from the ground up for asynchronous applications. Non-blocking I/O with SQLx and `pyo3-async-runtimes`.

</div>

## Quick Example

```python
import asyncio
from typing import Annotated
from ferro import Model, FerroField, ForeignKey, BackRef, connect

class Author(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    name: str
    posts: BackRef[list["Post"]] = None

class Post(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    title: str
    published: bool = False
    author: Annotated[Author, ForeignKey(related_name="posts")]

async def main():
    # Connect with auto-migration for development
    await connect("sqlite:blog.db?mode=rwc", auto_migrate=True)

    # Create records
    author = await Author.create(name="Jane Doe")
    post = await Post.create(
        title="Why Ferro is Fast",
        author=author,
        published=True
    )

    # Query with filters
    published_posts = await Post.where(
        Post.published == True
    ).order_by(Post.id, "desc").all()

    # Access relationships
    post_author = await post.author
    author_posts = await author.posts.all()

if __name__ == "__main__":
    asyncio.run(main())
```

## Why Ferro?

Traditional Python ORMs pay a **performance tax** for SQL generation, row parsing, and object instantiation — all happening in Python with the GIL held. Ferro moves these operations to a dedicated Rust core, delivering:

- **10-100x faster** bulk operations and complex queries
- **Zero-copy data paths** for maximum throughput
- **GIL-free I/O** for true async concurrency
- **Type-safe** with full IDE autocomplete

Still skeptical? [See the benchmarks](why-ferro.md#benchmarks) or read about [how it works](concepts/architecture.md).

## Key Features

### High-Performance Core

All SQL generation and row hydration are handled by a dedicated Rust engine. Row data flows from SQLx → Rust → Python with minimal copying, bypassing the Python interpreter's overhead entirely.

### Identity Map

Ensures object consistency across your application. Fetch the same record twice, get the exact same Python object instance. Changes are immediately visible everywhere.

### Async Everything

Built on SQLx and `pyo3-async-runtimes`. No sync wrappers, no thread pools — true non-blocking database I/O from the ground up.

### Pydantic Integration

Define schemas with standard Pydantic models. Get validation, serialization, and JSON schema generation for free. Ferro extends Pydantic with database-specific constraints.

### Alembic Migrations

Production-ready schema management through Alembic. Ferro generates SQLAlchemy metadata automatically — no duplicate schema definitions.

## Ready to Start?

<div class="grid cards" markdown>

-   :material-clock-fast:{ .lg .middle } **5-Minute Tutorial**

    ---

    Build a working blog API with models, queries, and relationships.

    [:octicons-arrow-right-24: Get started](getting-started/tutorial.md)

-   :books:{ .lg .middle } **User Guide**

    ---

    Learn about models, relationships, queries, and transactions.

    [:octicons-arrow-right-24: Read the guide](guide/models-and-fields.md)

-   :material-api:{ .lg .middle } **API Reference**

    ---

    Complete reference for all classes, methods, and types.

    [:octicons-arrow-right-24: Browse API docs](api/model.md)

</div>

## Trusted By

Ferro is used in production by teams that need both developer ergonomics and runtime performance. [Read case studies →](https://github.com/syn54x/ferro-orm/discussions)

## Community

- **GitHub**: [syn54x/ferro-orm](https://github.com/syn54x/ferro-orm)
- **Discussions**: Ask questions and share projects
- **Contributing**: [Contribution guide](contributing.md)
- **License**: Apache 2.0
