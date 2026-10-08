# Model

`Model` is the base class every Ferro model inherits from. The lifecycle is: define a subclass with annotated fields (which registers its table schema), [`connect()`](connection.md) to a database, then perform CRUD through classmethods (`create`, `get`, `where`, ...) and instance methods (`save`, `delete`, `refresh`). Because `Model` is a Pydantic model, instances validate on construction and serialize like any other Pydantic object.

::: ferro.Model

## Table-level declarations

A model declares its table checks in `__ferro_checks__` and its row security in `__ferro_rls__`; see [Table checks](../guide/models-and-fields.md#table-checks) and [Using with Postgres RLS](../guide/row-level-security.md).

::: ferro.Check

::: ferro.RowSecurity

::: ferro.RowPolicy
