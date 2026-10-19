# execute, fetch_all and fetch_one

Escape hatches for SQL the query builder does not cover. All three functions take an SQL string and positional bind parameters (placeholders are `?` on SQLite and `$1`, `$2`, ... on Postgres) and honor an active `transaction()` block. See the [Raw SQL guide](../guide/raw-sql.md).

::: ferro.execute

::: ferro.fetch_all

::: ferro.fetch_one
