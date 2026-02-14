# Next Steps

Congratulations on completing the tutorial! You now have a solid foundation in Ferro. Here's where to go next based on your goals.

## Learn by Use Case

### Building an API

If you're building a REST API with FastAPI, Starlette, or similar:

1. **[Models & Fields](../guide/models-and-fields.md)** — Learn about all field types and constraints
2. **[Relationships](../guide/relationships.md)** — Master one-to-many, one-to-one, and many-to-many
3. **[Queries](../guide/queries.md)** — Advanced filtering, ordering, and pagination
4. **[How-To: Pagination](../howto/pagination.md)** — Implement efficient pagination
5. **[Transactions](../guide/transactions.md)** — Ensure data consistency

### Data Processing

If you're processing large datasets or building ETL pipelines:

1. **[Mutations](../guide/mutations.md)** — Bulk operations for high throughput
2. **[Queries](../guide/queries.md)** — Efficient filtering and aggregation
3. **[Performance](../concepts/performance.md)** — Optimization techniques
4. **[Transactions](../guide/transactions.md)** — Atomic operations

### Production Deployment

If you're ready to deploy to production:

1. **[Database Setup](../guide/database.md)** — Connection pooling and configuration
2. **[Schema Management](../guide/migrations.md)** — Alembic migrations workflow
3. **[How-To: Testing](../howto/testing.md)** — Build a comprehensive test suite
4. **[How-To: Multiple Databases](../howto/multiple-databases.md)** — Read replicas and sharding

### Understanding Internals

If you want to understand how Ferro works:

1. **[Architecture](../concepts/architecture.md)** — The Rust bridge and data flow
2. **[Identity Map](../concepts/identity-map.md)** — Instance caching and consistency
3. **[Type Safety](../concepts/type-safety.md)** — Pydantic integration details
4. **[Performance](../concepts/performance.md)** — Where Ferro is fast and why

## Common Patterns

### Timestamps

Add `created_at` and `updated_at` to all models:

```python
from datetime import datetime
from ferro import Model, Field

class BaseModel(Model):
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

class User(BaseModel):
    username: str
    email: str
```

[Learn more →](../howto/timestamps.md)

### Soft Deletes

Implement "soft delete" pattern:

```python
class User(Model):
    username: str
    is_deleted: bool = False
    deleted_at: datetime | None = None

# Query only non-deleted
active_users = await User.where(User.is_deleted == False).all()
```

[Learn more →](../howto/soft-deletes.md)

### Pagination

Implement cursor-based pagination for large datasets:

```python
def paginate_users(after_id: int | None = None, limit: int = 20):
    query = User.select()
    if after_id:
        query = query.where(User.id > after_id)
    return query.order_by(User.id).limit(limit)

users = await paginate_users(after_id=100, limit=20).all()
```

[Learn more →](../howto/pagination.md)

## Reference Material

### API Reference

Complete reference for all classes and methods:

- [Model API](../api/model.md)
- [Query API](../api/query.md)
- [Field API](../api/fields.md)
- [Relationship API](../api/relationships.md)

### User Guide

In-depth guides for all features:

- [Models & Fields](../guide/models-and-fields.md)
- [Relationships](../guide/relationships.md)
- [Queries](../guide/queries.md)
- [Mutations](../guide/mutations.md)
- [Transactions](../guide/transactions.md)
- [Database Setup](../guide/database.md)
- [Schema Management](../guide/migrations.md)

## Get Help

### Community

- **GitHub Discussions**: Ask questions, share projects
- **Issues**: Report bugs or request features
- **Contributing**: Help improve Ferro

[Join the community →](https://github.com/syn54x/ferro-orm/discussions)

### FAQ

Common questions and answers:

- How does Ferro compare to SQLAlchemy?
- Do I need to know Rust?
- Can I use Ferro with FastAPI?
- Is Ferro production-ready?

[Read the FAQ →](../faq.md)

## Stay Updated

- **GitHub**: Star [syn54x/ferro-orm](https://github.com/syn54x/ferro-orm) for updates
- **Changelog**: Track new features and fixes
- **Twitter**: Follow [@ferroorm](https://twitter.com/ferroorm) for announcements

## Start Building

The best way to learn is by building something real. Pick a project and dive in!

Need inspiration? Here are some project ideas:

- 📝 **Blog Platform** — Users, posts, comments, tags
- 🛒 **E-commerce API** — Products, orders, inventory
- 📊 **Analytics Dashboard** — Events, metrics, aggregations
- 💬 **Chat Application** — Users, messages, channels
- 🎫 **Ticket System** — Issues, comments, attachments

Happy coding with Ferro! 🚀
