# Typed Query Predicates

This page explains how Ferro's lambda predicates are checked: what your type checker sees, what Ferro checks when you build a query, and where checking stops.

`Model.where()`, `Query.where()` and a relation's `where()` take one kind of argument, a lambda whose parameter stands for one row of the model:

```python {test=run prelude=queries}
uncleared_debits = await Transaction.where(
    lambda transaction: (transaction.amount < 0)
    & (transaction.cleared_on == None)  # noqa: E711
).all()
assert {transaction.memo for transaction in uncleared_debits} == {
    "Train to Lyon",
    "Dinner in Lyon",
    "Weekly shop",
}
```

Each comparison in the lambda returns a condition, and `where()` turns the conditions into the query's `WHERE` clause. The examples on this page use the ledger from the [Queries guide](../guide/queries.md#the-ledger) and run inside a [session](../guide/connections.md#sessions).

## Proxy validation

The lambda never sees a `Transaction`. Ferro calls it with a [`QueryProxy`][ferro.query.QueryProxy] for the model, whose attributes are the model's columns, including the `<relation>_id` column each foreign key adds. Reading an attribute returns a `FieldProxy`, and comparing a `FieldProxy` returns a condition, a `QueryNode`.

The proxy checks every name when the lambda runs, which is when you build the query. A misspelled column raises `AttributeError` with the closest match and the full list, before anything reaches the database:

```python {test=run prelude=queries}
try:
    Transaction.where(lambda transaction: transaction.cleard_on == None)  # noqa: E711
except AttributeError as error:
    assert "Did you mean 'cleared_on'?" in str(error)
else:
    raise AssertionError("a misspelled column must raise AttributeError")
```

The query is never built, so no SQL runs.

## What the type checker sees

`where()` is typed to take a [`Predicate`][ferro.query.Predicate]: `Callable[[QueryProxy[TModel]], QueryNode]`. That gives two guarantees:

- **A valid predicate type-checks.** `lambda transaction: transaction.amount < 0` passes `ty check` and Pyright, because `<` on a `FieldProxy` is typed to return `QueryNode`.
- **A predicate that returns something else fails the checker.** `lambda transaction: True` returns a `bool`, not a `QueryNode`, so it is a type error rather than a query that matches every row.

A relation's `where()`, such as `account.transactions.where(...)`, takes the same `Predicate`:

```python {test=run prelude=queries}
checking = await Account.get(1)
uncleared = await checking.transactions.where(
    lambda transaction: transaction.cleared_on == None  # noqa: E711
).all()
assert [transaction.memo for transaction in uncleared] == ["Weekly shop"]
```

Weekly shop is the one uncleared transaction on Checking.

## Traversing relations

A predicate that follows a foreign key, such as `transaction.account.owner.name == "Ada Lovelace"`, type-checks for the same reason: each attribute returns another proxy, and the final comparison returns `QueryNode`. The [Queries guide](../guide/queries.md#querying-across-relationships) covers what a traversal does to the query.

The checker checks the shape of the predicate, and the proxy checks the names in it:

- **A traversal that ends in a comparison type-checks**, because it returns a `QueryNode`.
- **A bare relation fails the checker.** `lambda transaction: transaction.account` returns a proxy, not a `QueryNode`, the same failure as `lambda transaction: True`.
- **A misspelled step type-checks, and fails when you build the query.** To the checker, `transaction.accont` is a `FieldProxy` like any other attribute; at build time the proxy checks each step against its own model and raises `AttributeError` naming that model.

## Projected queries

`select()` with a selector changes the query's type from `Query[Transaction]` to `ProjectedQuery[Transaction]`, so the terminals change type with it: `all()` checks as [`Rows[Row]`][ferro.query.Rows] and `first()` as [`Row`][ferro.query.Row]` | None`. A bare `select()` keeps `Query[Transaction]`:

```python {test=run prelude=queries}
from ferro import Row, Rows

records = await Transaction.select(
    lambda transaction: (transaction.id, transaction.amount)
).all()  # Rows[Row]
first = await Transaction.select(lambda transaction: transaction.memo).first()  # Row | None
everything = await Transaction.select().all()  # list[Transaction]

assert isinstance(records, Rows)
assert isinstance(first, Row)
assert isinstance(everything[0], Transaction)
```

Each result has the type its annotation claims.

A selected query returns records because a Ferro model instance always holds a whole row. If `select()` returned a `Transaction` with two of its seven columns loaded, every `save()`, every refresh and every function that takes a `Transaction` would have to know which query produced its argument, and reading an unloaded field would fail far from that query. A `Row` says what it is: the columns you asked for and nothing else. The query carries its result shape with it, and records are built on the same no-validation path as model instances, so selecting fewer columns is never slower per row than selecting all of them.

Every selector form keeps that type: a tuple, a single field, a traversal at any depth, and a dict, whose values may also be aggregates:

```python {test=run prelude=queries}
totals = await Transaction.select(
    lambda transaction: {
        "account_id": transaction.account_id,
        "total": transaction.amount.sum(),
    }
).all()  # Rows[Row]
assert len(totals) == 4
```

The four accounts each get one record with their total.

An aggregate such as `transaction.amount.sum()` is an [`AggregateExpr`][ferro.query.AggregateExpr]: not a value, not a `QueryNode` and not a column. That is what lets Ferro refuse its misuse: comparing an aggregate inside `where()` raises when you build the query, and an `AggregateExpr` is valid only as a dict value in a selector or as the result of an `order_by()` lambda.

The work divides as it does for predicates:

- **The checker checks the shape.** Assigning a `Rows[Row]` to a variable typed `list[Transaction]` is an `invalid-assignment` error, and calling `update()` or `delete()` on a selected query is a type error at the call as well as a `ValueError` when it runs.
- **The proxy checks the names.** A selected column is a `FieldProxy` to the checker; a misspelled one raises `AttributeError` when you build the query.

The checker knows a selected query returns `Rows[Row]`, but not which fields a given `Row` has: `row.total` types as `Any`. Typing each record's fields from its selector needs the type features described under [Where checking stops](#where-checking-stops).

## Included queries

`include()` does not change the query's type. An included query is still a `Query[Transaction]`: `all()` checks as `list[Transaction]`, `first()` as `Transaction | None`, and a populated relation (`transactions[0].account.label`) type-checks through the field's own annotation. There is no `Loaded[Transaction]` type.

`include()` keeps the model type for two reasons:

- **A "loaded" type would be wrong under the identity map.** In a session, an included query returns the same objects a plain query returns: populations attach to shared instances and accumulate across queries. A type that says "this object's `account` is populated" claims something the next refresh may undo (see [Population after a refresh](../guide/queries.md#population-after-a-refresh)), and a plain query can hand you an object an earlier query populated. The one type that is always true of an instance is its model.
- **The annotation already names the instance.** The field says `account: Account`. A separate loaded type would have to rewrite each field's type on the unloaded side (`account: Awaitable[Account]` to `Account`), which needs type features Python does not have yet; see [Where checking stops](#where-checking-stops). Populating a relation is what makes the declared annotation true, where you asked for it.

The misuses a checker can catch do fail it: `include()` on a selected query is a type error at the call, and a string selector fails the lambda parameter type.

## Where checking stops

The checker does not check the right-hand side of a comparison. A proxy attribute is a `FieldProxy[Any]`, so `transaction.amount < "a lot"` type-checks and fails only when it runs. The same gap leaves record fields typed `Any` and rules out a "loaded" model type.

Closing it needs per-field types on the proxy: TypeScript-style mapped types, which [PEP 827](https://peps.python.org/pep-0827/) ("Type Manipulation", a draft) proposes for Python. Once type checkers support them, `QueryProxy` attributes can carry each field's declared type, record fields can carry their selected types, and `transaction.amount < "a lot"` will fail the checker. None of that needs a change at runtime, because the runtime already has one type per result.

## Unchanged behavior

Lambda predicates change nothing outside the query:

- Your model annotations stay as they are: `cleared_on: date | None = None` is still a plain Pydantic field.
- Pydantic's schema generation, JSON schema output and validation are unchanged.
- Predicates reach the Rust core through the same query envelopes as every other query.

## Reference

- [`QueryProxy`][ferro.query.QueryProxy]: the object a predicate's parameter receives
- [`Predicate`][ferro.query.Predicate]: the type of a lambda predicate
- [`RowSelector`][ferro.query.RowSelector]: the type of a `select()` lambda
- [`AggregateExpr`][ferro.query.AggregateExpr]: the type of `transaction.amount.sum()` and the other aggregates
- [`Row`][ferro.query.Row] and [`Rows`][ferro.query.Rows]: the records a selected query returns

## See also

- [Queries guide](../guide/queries.md)
- [Type safety](type-safety.md)
