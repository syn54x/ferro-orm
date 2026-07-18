# Predicate negation is uniform `~`, a NOT node in the wire

Any predicate node — leaf comparison, AND/OR compound, or existence test — is
negated by prefix `~`, carried on the wire as a dedicated NOT node wrapping its
child and rendered via the SQL `NOT`. There are no per-operator negative forms
beyond the comparison pairs that already exist (`==`/`!=`, `<`/`>=`, …): no
`not_in`, no `not_like`, no `negated` flag on the existence-test node.

The alternative — negation only where an operator pair happens to exist — is
not a rule but a lookup table with holes: `!=` for equality, nothing for `IN`,
nothing for `LIKE`, and some third spelling for NOT EXISTS. The first thing a
user who learns `~t.lines.exists()` will try is `~t.category_id.in_(ids)`, and
it should work. Build-time De Morgan expansion was rejected because it cannot
close: `IN` and `LIKE` have no negative-operator forms to expand into, so that
path forces exactly the per-operator proliferation it was meant to avoid. A
wire NOT node needs none of that — `NOT (x IN (...))` is already correct SQL.

## Consequences

- One universal rule to document: prefix `~` negates any predicate.
- The pre-existing NOT IN / NOT LIKE gap closes for free (`~t.col.in_(ids)`,
  `~t.col.like(p)`).
- SQL three-valued logic must be documented once, centrally: `~(t.amount > 5)`
  excludes rows where `amount` is NULL, exactly as SQL `NOT` and the existing
  `!=` spelling do — Python set-complement intuition does not apply. A docs
  note with rendered SQL covers it.
- The wire grows one recursive node kind (`not`); golden vectors pin it. Every
  future predicate form gets negation without further design.
- `QueryNode.__bool__`'s existing guard against `not node` misuse can point at
  `~` as the supported spelling.
