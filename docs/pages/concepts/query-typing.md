# Typed Query Predicates

`Model.where`, `Query.where`, and `Relation.where` accept a single predicate
shape: a lambda that receives a `QueryProxy` and returns a `QueryNode`.

```python
rows = await User.where(lambda user: user.archived == False).all()
rows = await User.where(
    lambda user: (user.role == "admin") & (user.active == True)
).all()
```

## How It Works

The lambda receives a fresh `QueryProxy` for the model being queried.
Attribute access on the proxy is validated against the model's declared
columns (plus shadow `{fk}_id` columns) and returns a `FieldProxy`, so
`user.archived == False` builds a `QueryNode`. A misspelled column —
`user.archievd` — raises `AttributeError` naming the closest valid match and
listing every queryable column, at build time, before any query reaches the
database.

Name the lambda parameter after the model in lowercase singular (`user` for
`User`, `post` for `Post`) so predicates read like English. The full operator
surface is available: `==`, `!=`, `<`, `<=`, `>`, `>=`, `.like()`, `.in_()`,
`&`, `|`, `== None`, and shadow FK columns (`user.author_id`).

That gives you two concrete guarantees today:

- **A valid predicate type-checks as `QueryNode`.** `lambda user: user.age >= 18` passes `ty check` / Pyright because `>=` on a `FieldProxy` is typed to return `QueryNode`, which is exactly what `where()` expects.
- **A junk predicate fails the checker.** `lambda user: True` — a callable that doesn't return a `QueryNode` at all — is a type error, not a silent no-op query, because `where()`'s parameter type is `Predicate = Callable[[QueryProxy[TModel]], QueryNode]`.

What isn't checked yet is the *right-hand side* of a comparison: the proxy attribute type is `FieldProxy[Any]`, so `user.age >= "eighteen"` type-checks even though it would fail at runtime. Closing that gap needs per-field static types on the proxy — TypeScript-style mapped types, proposed for Python in [PEP 827](https://peps.python.org/pep-0827/) ("Type Manipulation", draft status, targeting Python 3.16). When type checkers support it, `QueryProxy` attribute typing upgrades from `FieldProxy[Any]` to each field's real declared type — with zero runtime change — and `user.age >= "eighteen"` starts failing the checker too.

`Relation.where` (used on `BackRef` collections) accepts the same shape:

```python
published = await author.posts.where(lambda post: post.published == True).all()
```

## Traversing Relations

Reaching through a foreign key inside a predicate — `lambda transaction: transaction.account.ledger_id == 1` — type-checks as a predicate for the same reason a plain comparison does: attribute access on the proxy keeps returning a chainable proxy, and the terminal comparison returns `QueryNode`. The checker sees a valid `Predicate[Transaction]`; the [Queries guide](../guide/queries.md#querying-across-relationships) covers what the traversal *means* (one INNER join per relation path).

The static gate checks the **shape** of the predicate, not the **names** in it:

- **A traversal that ends in a comparison type-checks.** `transaction.account.ledger_id == 1` is a `QueryNode`, so it satisfies `where()`.
- **A bare relation as a predicate fails the checker.** `lambda transaction: transaction.account` returns a proxy, not a `QueryNode`, so it is a type error — the same failure mode as `lambda user: True`.
- **Name typos are caught at build time, not by the checker.** A misspelled hop (`transaction.accont`, `account.emial`) is `FieldProxy[Any]` to the type checker, so it type-checks; at build time the proxy validates every hop against the real model and raises `AttributeError` with a did-you-mean naming that hop's model. The static gate guarantees shape; the runtime proxy guarantees names.

## Projected Queries

Partial selects extend the same shape-not-names promise through projection.
`select(...)` with a selector flips the query's static type from
`Query[Model]` to `ProjectedQuery[Model]`, so the terminals change shape:
`.all()` checks as `Rows[Row]` and `.first()` as `Row | None` — never
`list[Model]`:

```python
rows = await Transaction.select(lambda t: (t.id, t.amount)).all()  # Rows[Row]
row = await Transaction.select(lambda t: t.amount).first()  # Row | None
full = await Transaction.select().all()  # list[Transaction] — bare form unchanged
```

Every selector form keeps that shape: the tuple, the single field, traversal
at any depth (`t.account.owner.email` chains as `FieldProxy[Any]`), and the
dict form, whose values may also be **aggregate expressions**:

```python
row = await Transaction.select(
    lambda t: {"acct": t.account_id, "total": t.amount.sum()}
).first()  # Row | None
```

An aggregate expression types **opaquely**: `t.amount.sum()` is an
`AggregateExpr` — deliberately not a value, not a `QueryNode`, not a column.
That is what makes the misuse gates work: an aggregate compared inside
`where()` raises at build time (post-aggregation filtering is `having()`),
and an `AggregateExpr` is only ever valid as a dict-selector value or an
`order_by()` lambda result.

The gate divides the work exactly like predicates do:

- **Shape is checked statically.** Passing a `Row` where a model instance is
  expected fails the checker (`list[Transaction] = await
  Transaction.select(lambda t: (t.id,)).all()` is an `invalid-assignment`),
  and mutating a projected query (`.update()` / `.delete()`) is a static
  error at the call site as well as a build-time `ValueError`.
- **Names are checked at build time.** A selected column is `FieldProxy[Any]`
  to the checker; a misspelled column raises `AttributeError` with a
  did-you-mean when the query is built, before any round-trip — the same
  runtime validation as `where()` and `order_by()`.

The promise is **shape, not names**: the checker knows a projected query
yields `Rows[Row]`, but not that this particular `Row` has a `.total` of type
`int | None` — record fields type as `Any` on access. Inferring per-field
record types from the selector (so `row.total` checks as what the aggregate
contract says it is) is the record-field inference lane, tracked in
[#290](https://github.com/syn54x/ferro-orm/issues/290) — another place
typing can sharpen later with zero runtime change.

## Included Queries

`include()` deliberately does **not** flip the query's type the way `select()`
does. An included query stays `Query[Model]`-shaped: `.all()` checks as
`list[Transaction]`, `.first()` as `Transaction | None`, and populated access
(`txns[0].account.label`) type-checks through the field's ordinary declared
annotation. There is no `Loaded[Transaction]`.

That is a decision, not a gap, for two reasons:

- **A per-query "loaded" type is unsound under the identity map.** In a
  session, the instance an included query returns is the *same object* a
  plain query returns — populations attach to shared instances and accumulate
  across queries. A static brand that says "this object's `account` is
  populated" asserts something the runtime cannot pin to a type: the next
  refresh may drop the population (see the
  [refresh rule](../guide/queries.md#refreshes-drop-populations-that-stopped-being-true)),
  and a plain query can hand you the already-populated object. The honest
  static type of every instance is the model itself.
- **The declared annotation already claims the instance.** The field says
  `account: Account`; a distinct loaded type would need to *transform*
  per-field types on the unloaded side instead (`account: Awaitable[Account]`
  → `Account`), which requires TypeScript-style mapped types —
  [PEP 827](https://peps.python.org/pep-0827/) (draft, targeting
  Python 3.16). Population is what makes the declared annotation *true* at
  runtime, exactly where the user opted in.

When PEP 827 lands, typing can sharpen with zero runtime change — precisely
because the runtime type stayed single. Meanwhile the statically catchable
misuses do fail the gate: `include()` on a projected query is an error at the
call site (the `self: Never` pin), and a string selector fails the callable
parameter type.

## What This Doesn't Change

- Your model annotations. `archived: bool = False` stays exactly as it is.
- Pydantic schema generation, JSON schema output, or model validation.
- The Rust FFI bridge architecture (predicates serialize through QueryIR envelopes).

## Reference

- `ferro.query.QueryProxy` — validating attribute proxy passed to lambda predicates.
- `ferro.query.Predicate` — `Callable[[QueryProxy[TModel]], QueryNode]`, the type of any lambda predicate.
- `ferro.query.FieldProxy` — generic over the column's Python type (`FieldProxy[T]`); the mechanism a `QueryProxy` attribute access returns.
- `ferro.query.RowSelector` — the type of a `select()` lambda selector: a callable returning one field, a tuple of fields, or a dict of output names to fields/aggregates.
- `ferro.query.AggregateExpr` — the opaque type of `t.amount.sum()` and friends; valid only as a dict-selector value or an `order_by()` lambda result.
- `ferro.query.Row` / `ferro.query.Rows` — projected records and their list-like container, the result shape of a projected query.

## See Also

- [Queries Guide](../guide/queries.md)
- [Type Safety](type-safety.md)
