"""Type-checked (never executed): valid lambda predicates resolve to QueryNode."""

from typing import Annotated, assert_type

from ferro import FerroField, Model
from ferro.query import FieldProxy, Predicate, QueryNode, QueryProxy


class GoodUser(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    age: int = 0
    archived: bool = False


pred_compare: Predicate[GoodUser] = lambda u: u.age >= 18
pred_compound: Predicate[GoodUser] = lambda u: (u.age >= 18) & (u.archived == False)  # noqa: E712
pred_in: Predicate[GoodUser] = lambda u: u.age.in_([1, 2, 3])

assert_type(pred_compare(QueryProxy[GoodUser](GoodUser)), QueryNode)
assert_type(FieldProxy("age") >= 18, QueryNode)
