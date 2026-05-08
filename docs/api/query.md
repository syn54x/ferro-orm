# Query API

Complete reference for the Query Builder API.

## `Query`

`Query.where` accepts either a `QueryNode` (the operator and `col()` paths) or a lambda predicate of shape `Callable[[QueryProxy[TModel]], QueryNode]`. See [Typed Query Predicates](../concepts/query-typing.md) for a full treatment of the three predicate styles.

::: ferro.query.Query
    options:
      members:
        - where
        - order_by
        - limit
        - offset
        - all
        - first
        - count
        - exists
        - update
        - delete
      show_source: false
      heading_level: 3

## `Relation`

`Relation` is the lazy collection-relationship subclass of `Query` returned by `BackRef` and `ManyToMany` fields. It accepts the same three predicate styles on `where`.

::: ferro.query.Relation
    options:
      members:
        - where
        - order_by
        - limit
        - offset
        - all
        - first
        - add
        - remove
        - clear
      show_source: false
      heading_level: 3

## `col`

Runtime-identity wrapper that statically narrows a model class attribute back to `FieldProxy[T]`. Use it when a single attribute on an existing chain trips your type checker; for new code prefer the lambda predicate style.

::: ferro.query.col
    options:
      show_source: false
      heading_level: 3

## `FieldProxy`

The typed proxy installed by Ferro's metaclass on every model class field. Generic over the column's Python type; operator overloads accept `T | FieldProxy[T]` and return `QueryNode`.

::: ferro.query.FieldProxy
    options:
      members:
        - in_
        - like
      show_source: false
      heading_level: 3

## `QueryProxy`

The attribute proxy passed to lambda predicates. Each attribute access returns a fresh `FieldProxy` for the accessed name.

::: ferro.query.QueryProxy
    options:
      show_source: false
      heading_level: 3

## `QueryNode`

The serializable AST node produced by every predicate style. You normally do not construct these directly.

::: ferro.query.QueryNode
    options:
      members:
        - to_dict
      show_source: false
      heading_level: 3

## `Predicate`

Type alias for lambda predicates accepted by `Query.where`, `Relation.where`, and `Model.where`.

```python
Predicate[TModel] = Callable[[QueryProxy[TModel]], QueryNode]
```

## See Also

- [Queries Guide](../guide/queries.md)
- [Typed Query Predicates](../concepts/query-typing.md)
- [How-To: Pagination](../howto/pagination.md)
