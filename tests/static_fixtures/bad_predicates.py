"""Type-checked (never executed): junk predicates must FAIL `ty check`.

test_static_contracts.py asserts this file produces `invalid-assignment`
diagnostics -- if it starts passing, the static gate has stopped biting.
"""

from typing import Annotated

from ferro import FerroField, Model
from ferro.query import Predicate


class BadUser(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    age: int = 0


bad_bool: Predicate[BadUser] = lambda u: True          # bool is not QueryNode
bad_value: Predicate[BadUser] = lambda u: u.age        # FieldProxy is not QueryNode
