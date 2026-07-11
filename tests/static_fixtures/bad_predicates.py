"""Type-checked (never executed): junk predicates must FAIL `ty check`.

test_static_contracts.py asserts this file produces `invalid-assignment`
diagnostics -- if it starts passing, the static gate has stopped biting.
"""

from typing import Annotated

from ferro import FerroField, ForeignKey, Model
from ferro.query import Predicate


class BadUser(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    age: int = 0


bad_bool: Predicate[BadUser] = lambda u: True          # bool is not QueryNode
bad_value: Predicate[BadUser] = lambda u: u.age        # FieldProxy is not QueryNode


# Relation-proxy guardrails (#273). Attribute access on the QueryProxy is
# statically `FieldProxy[Any]` (the chainable shape — a relation only resolves
# to a RelationProxy at runtime), so:
class BadAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None


class BadTxn(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    account: Annotated[BadAccount, ForeignKey(related_name="txns")]


# Bare relation as a predicate: `t.account` is a relation, not a QueryNode.
# Types as FieldProxy[Any], so the assignment fails with invalid-assignment —
# the same static failure mode as `bad_value` above.
bad_bare_relation: Predicate[BadTxn] = lambda t: t.account

# Relation-vs-scalar comparison: `t.account == 5` type-checks (FieldProxy.__eq__
# returns QueryNode under the shape-only static promise, PRD #267). Its
# guardrail is a RUNTIME TypeError raised by RelationProxy.__eq__ (a relation
# compares only to a target instance or None), exercised by test_query_joins.
# Pinned here to document that relation-misuse is a build-time (runtime) error,
# not a static one — this line does NOT produce a diagnostic.
bad_relation_scalar: Predicate[BadTxn] = lambda t: t.account == 5
