# Existence tests & uniform negation — design

**Date:** 2026-07-18
**Origin:** external PRDs #307 (reverse-relation membership predicates) and #308 (OR-composition across a reverse child table), both from Pinch. The SQL shapes in those PRDs are requirements; the Python spelling was redesigned.
**Decisions recorded in:** ADR-0007 (reverse relations are tested, not traversed), ADR-0008 (predicate negation is uniform `~`).
**Glossary:** `CONTEXT.md` → *Existence test*.

## Problem

A root-model query cannot filter on membership in a reverse (BackRef) or
many-to-many relation. `build_relation_specs` registers forward FKs only, so
reverse edges are invisible to predicate traversal, `left_join()`, and every
other query surface — both PRD repros die in `AttributeError`. The predicate
tree also has no negation beyond per-operator pairs: no NOT IN, no NOT LIKE,
and no way to spell NOT EXISTS.

The blocked workloads (Pinch M6/M8) need, composed with ordinary root
predicates and keyset pagination:

```sql
-- #307: is_transfer filter (true / false)
[NOT] EXISTS (SELECT 1 FROM transfer tr
              WHERE tr.outflow_transaction_id = t.id
                 OR tr.inflow_transaction_id = t.id)

-- #308: line-aware category filter, child-less roots kept by the OR
t.category_id = ANY($2)
OR EXISTS (SELECT 1 FROM split_line l
           WHERE l.transaction_id = t.id AND l.category_id = ANY($2))
```

Root-set semantics are the requirement: no duplicated roots when the relation
is to-many, no dropped roots on the other branch of an OR.

## The mechanism

Two features, separately shippable, the second building on the first:

### 1. Uniform negation (`~`)

Prefix `~` negates **any** predicate node — leaf, AND/OR compound, existence
test. Wire: a new recursive `not` node kind wrapping its child. Rust: one
recursion case in the condition builder (SeaQuery `Cond::not`). No per-operator
negative forms are added; `~t.col.in_(ids)` and `~t.col.like(p)` close the
pre-existing NOT IN / NOT LIKE gap for free. Rationale and rejected
alternatives (per-operator forms, De Morgan expansion): ADR-0008.

Three-valued logic is documented centrally: `~(t.amount > 5)` excludes rows
where `amount` IS NULL, exactly like SQL `NOT` and the existing `!=`.

### 2. Existence tests (`.exists()`)

The only predicate form on a reverse or M2M relation:

```python
# 307
Txn.where(lambda t: t.transfer_out.exists() | t.transfer_in.exists())
Txn.where(lambda t: ~t.transfer_out.exists() & ~t.transfer_in.exists())

# 308
Txn.where(lambda t: t.category_id.in_(ids)
                  | t.lines.exists(lambda l: l.category_id.in_(ids)))
```

- **Uniform verb, uniform rendering.** `.exists()` at every cardinality
  (one-to-one BackRefs included — no `.has()` split, no LEFT JOIN
  specialization) and for both relation kinds (reverse FK and M2M). Always a
  correlated `EXISTS`.
- **Inner predicate = full ferro predicate over the related model.** Same
  operators, forward traversal (joins *inside* the subquery, ADR-0006
  semantics unchanged), nested `.exists()` to arbitrary depth. The explicit
  lambda scope is what makes grouping unambiguous —
  `t.lines.exists(lambda l: A & B)` (one child row matches both) vs
  `t.lines.exists(A) & t.lines.exists(B)` (some child row each) — the
  Django-style implicit-traversal ambiguity ADR-0007 rejects.
- **Cross-scope references rejected (v1).** An inner-tree leaf built from any
  proxy other than the inner lambda's parameter, or a `FieldProxy` as a
  comparison RHS, is a build-time error. Deferred, tracked as its own issue
  (needs an owner-scope marker on leaf IR + outer-alias qualification in the
  render).
- **`.exists()` is the only verb.** Reverse-proxy column access, comparisons
  (including `!= None` / `== None`), and `in_` raise at build time with the
  supported spelling in the message.

## Architecture

**Python.**
- A reverse-spec map derived once at the compile choke point (beside
  `__ferro_relation_specs__`), from facts `resolve_relationships` already
  computes: related model, child FK column (reverse FK) or join-table triple
  (`join_table`, `source_col`, `target_col`; M2M), `is_one_to_one`.
- `QueryProxy.__getattr__` consults it ahead of the column fallback and
  returns a reverse-relation proxy exposing `.exists()` only.
- The inner lambda resolves through the existing predicate resolver against
  the related model.
- `QueryNode.__invert__` wraps in a not-node; `QueryNode.__bool__`'s guard
  message points at `~`.

**Wire (QueryIR version bump, hand-authored golden vectors).** Two new
recursive node kinds beside `leaf`/`compound`:
- `not {child}`
- `exists {hops, where}` — `hops` reuses the `QueryJoinHop` vocabulary
  (`from_column` / `to_table` / `to_column`): one hop for a reverse FK
  (`FROM child WHERE child.fk = root.pk`), two for M2M (join table, then
  target). `where` is an ordinary nested condition tree (may itself contain
  `exists` / `not`).

**Rust.** One render loop for `exists`: first hop's table is the subquery
`FROM`, correlated to the enclosing scope's alias; remaining hops render as
inner joins inside the subquery; the inner `where` builds through the existing
condition builder recursively; emit via SeaQuery `Expr::exists`. `not` emits
`Cond::not`. M2M is therefore test surface, not new mechanism.

## Error surfaces

| Surface | Behavior |
|---|---|
| `t.lines.category_id`, `t.transfer_out != None`, reverse `in_` | build-time error naming `.exists()` |
| `left_join(lambda t: t.transfer_out)` | rejected (row-multiplication stays forbidden); error names `.exists()` |
| `include(lambda t: t.lines)` | unchanged — population is a separate future mechanism |
| `in_(Query/ProjectedQuery)` | still `TypeError`; message names `.exists()` when RHS is a query |
| Cross-scope reference in inner lambda | build-time error; names the deferral |
| M2M/reverse edge on a model without the relation | existing unknown-attribute error unchanged |

## Testing

- **Golden vectors:** `not` (leaf child, compound child), `exists` 1-hop,
  `exists` 2-hop (M2M), nested `exists`-in-`exists`, `not`-of-`exists`.
- **Integration, both dialects:** the #307 shapes (both `is_transfer`
  branches; a transaction in a transfer via either column found exactly once),
  the #308 shape (three matching lines → one root row; child-less root
  survives the OR; keyset `order_by` + `limit` compose), forward traversal
  inside the subquery, M2M bare + scoped, depth-2 nesting, `~in_` / `~like`.
- **Negative paths:** every error-surface row above.
- **Docs:** guide section under queries (Assignment + Annotated tabs for any
  model declarations, lambda style throughout), the three-valued-logic note,
  reference updates.

## Declined / deferred

**Declined permanently** (recorded in ADR-0007/0008): implicit reverse path
traversal; `.any()`/`.has()` cardinality split; `in_(single-column
ProjectedQuery)`; `!= None` existence sugar; unique-BackRef LEFT JOIN
specialization; per-operator negative forms and De Morgan expansion.

**Deferred, tracked:** cross-scope correlation (column-to-column comparison /
closing over the outer proxy).

## Delivery & tracking

Project #7 is closed; tracking is PRD issues + native sub-issues:

1. **PRD issue: uniform negation** (`/to-prd`), linked to #307/#308 —
   independently shippable, closes the NOT IN / NOT LIKE gap.
2. **PRD issue: existence tests** (`/to-prd`), linked to #307/#308, depends
   on the negation PRD.
3. Sub-issues under each via `/to-issues`, linked with GitHub native
   sub-issues.
4. Deferred-feature issue for cross-scope correlation, linking ADR-0007.
5. Maintainer comment on #307/#308: workload accepted, spelling redesigned
   per ADR-0007/0008; issues close when the existence-test PRD ships.
