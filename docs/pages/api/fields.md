---
description: "API reference for Field, FerroField, DbType and the other field metadata."
---

# Field, FerroField and DbType

Fields are declared as type annotations on a [`Model`](model.md), with `Field(...)` supplying database options (primary key, unique, index, defaults). The other entries on this page (`FerroField`, `FerroNullable`, `varchar`, `DbType` and `DbTypeToken`) describe the field metadata and the explicit column types `db_type=` takes. `fn` holds the database functions a generated column's expression or a check can call.

::: ferro.Field
    options:
      show_overloads: false
      parameter_headings: true

::: ferro.FerroField

::: ferro.FerroNullable

::: ferro.varchar

::: ferro.DbType

::: ferro.DbTypeToken

::: ferro.fn
