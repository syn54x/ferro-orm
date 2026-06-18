# Transactions

`transaction()` is an async context manager that runs everything inside the block on one connection, committing on exit and rolling back on exception. It yields a `Transaction` handle for raw SQL that must run on the transaction's connection. See the [Transactions guide](../guide/transactions.md) for semantics and patterns.

::: ferro.transaction

::: ferro.Transaction
