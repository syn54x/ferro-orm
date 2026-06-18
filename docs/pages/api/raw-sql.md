# Raw SQL

Escape hatches for SQL the query builder doesn't cover. All three functions take a SQL string plus positional bind parameters — placeholders are `?` on SQLite and `$1`, `$2`, ... on Postgres — and honor an active `transaction()` block. See the [Raw SQL guide](../guide/raw-sql.md).

::: ferro.execute

::: ferro.fetch_all

::: ferro.fetch_one
