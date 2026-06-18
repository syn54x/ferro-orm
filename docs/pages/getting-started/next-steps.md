# Next Steps

You've finished the [Quickstart](quickstart.md) and have a working Ferro app. Here's where to go next, based on what you're building.

## Learn by Use Case

### Building an API

For REST APIs with FastAPI, Starlette, or similar:

1. **[Models & Fields](../guide/models-and-fields.md)** — field types, constraints, and defaults
2. **[Relationships](../guide/relationships.md)** — foreign keys, back-references, many-to-many
3. **[Queries](../guide/queries.md)** — filtering with lambda predicates, ordering, and pagination
4. **[How-To: Pagination](../howto/pagination.md)** — efficient offset and cursor pagination
5. **[How-To: Testing](../howto/testing.md)** — fast, isolated tests with in-memory SQLite

### Data Processing

For ETL pipelines and bulk workloads:

1. **[Mutations](../guide/mutations.md)** — `bulk_create` and bulk update/delete for throughput
2. **[Transactions](../guide/transactions.md)** — make multi-step writes atomic
3. **[Queries](../guide/queries.md)** — filter in the database, not in Python

### Production Deployment

When you're ready to ship:

1. **[Connections & Databases](../guide/connections.md)** — connection URLs and pool configuration
2. **[Migrations](../guide/migrations.md)** — the Alembic workflow for evolving schemas
3. **[How-To: Multiple Databases](../howto/multiple-databases.md)** — working with more than one database
4. **[How-To: Testing](../howto/testing.md)** — a test suite you can trust before deploying

## Common Patterns

Recipes for things most applications need:

- **[Testing](../howto/testing.md)** — per-test database isolation and fixtures
- **[Pagination](../howto/pagination.md)** — offset- and cursor-based pagination
- **[Timestamps](../howto/timestamps.md)** — `created_at` / `updated_at` on every model
- **[Soft Deletes](../howto/soft-deletes.md)** — flag rows as deleted instead of removing them
- **[Multiple Databases](../howto/multiple-databases.md)** — connecting to more than one database
- **[Migrating from SQLAlchemy](../howto/migrate-from-sqlalchemy.md)** — a side-by-side translation guide

## Reference Material

- **API Reference** — complete documentation for every public class and method: [Model](../api/model.md), [Queries](../api/queries.md), [Fields & Types](../api/fields.md), [Relationships](../api/relationships.md)
- **Concepts** — how Ferro works under the hood: [Architecture](../concepts/architecture.md), [Identity Map](../concepts/identity-map.md), [Type Safety](../concepts/type-safety.md)
- **[Roadmap](../roadmap.md)** — what's implemented today and what's planned

## Get Help

- **[GitHub Issues](https://github.com/syn54x/ferro-orm/issues)** — report bugs or request features
- **[GitHub](https://github.com/syn54x/ferro-orm)** — star the repo to follow releases

The best way to learn from here is to build something real — a blog, a ticket tracker, an inventory API — and reach for the guides above as questions come up.
