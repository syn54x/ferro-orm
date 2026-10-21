---
description: "Ferro is an async Python ORM with Pydantic models and a Rust core, for SQLite and PostgreSQL."
---

# Ferro ORM

--8<-- "README.md:intro"

Declare a model once. The two tabs show the same model in Ferro's two equivalent field-declaration styles:

=== "Assignment"

    ```python
    --8<-- "docs/examples/readme_quickstart.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/readme_quickstart_annotated.py:models"
    ```

Connect, open a session, create two users and query the active ones:

=== "Assignment"

    ```python
    --8<-- "docs/examples/readme_quickstart.py:query"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/readme_quickstart_annotated.py:query"
    ```

Only `alice` is active, so the script prints:

<!-- prints: readme_quickstart.py:query -->
```text
Found 1 active users.
```

Every ORM call runs inside `async with engines.session():`; see [Sessions](guide/connections.md#sessions). `auto_migrate=True` creates the table on connect, which suits development and tests.

## Where to go next

- [Installation](getting-started/installation.md) covers Python versions, extras and building from source.
- [Quickstart](getting-started/quickstart.md) builds a small blog backend with models, queries, relationships and transactions.
- [Why Ferro](why-ferro.md) compares Ferro with SQLAlchemy, Django ORM and Tortoise, and lists the trade-offs.
- [Schema management](guide/schema/overview.md) explains auto-migrate and `ferro migrate`.
- [Models and fields](guide/models-and-fields.md), [Queries](guide/queries.md) and [Relationships](guide/relationships.md) are the main guides.
- [How-to guides](howto/testing.md) hold recipes for testing, pagination, timestamps, soft deletes and multiple databases.
- [API reference](api/model.md) lists every public class and method.
- [Roadmap](roadmap.md) lists the features that are not implemented yet.
