"""Type-checked (never executed): valid lambda predicates resolve to QueryNode."""

from typing import Annotated, assert_type

from ferro import FerroField, ForeignKey, Model
from ferro.query import FieldProxy, Predicate, Query, QueryNode, QueryProxy


class GoodUser(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    age: int = 0
    archived: bool = False


pred_compare: Predicate[GoodUser] = lambda u: u.age >= 18
pred_compound: Predicate[GoodUser] = lambda u: (u.age >= 18) & (u.archived == False)  # noqa: E712
pred_in: Predicate[GoodUser] = lambda u: u.age.in_([1, 2, 3])

assert_type(pred_compare(QueryProxy[GoodUser](GoodUser)), QueryNode)
assert_type(FieldProxy("age") >= 18, QueryNode)


# Relation traversal (#270): attribute access on a forward-FK field is
# statically chainable, and a comparison at any hop still yields a QueryNode.
class GoodLedger(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None


class GoodOwner(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    email: str = ""


class GoodAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    ledger: Annotated[GoodLedger, ForeignKey(related_name="accounts")]
    owner: Annotated[GoodOwner, ForeignKey(related_name="accounts")]


class GoodTxn(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    account: Annotated[GoodAccount, ForeignKey(related_name="txns")]


pred_hop1: Predicate[GoodTxn] = lambda t: t.account.ledger_id == 1
pred_hop_multi: Predicate[GoodTxn] = lambda t: t.account.owner.email == "a@b.com"
pred_hop_compound: Predicate[GoodTxn] = lambda t: (t.account.ledger_id == 1) & (
    t.account.owner.email == "a@b.com"
)
assert_type(pred_hop1(QueryProxy[GoodTxn](GoodTxn)), QueryNode)


# Explicit join()/left_join() chainers (#272): the selector names a RELATION
# path (`lambda t: t.account`) using the same chainable proxy shape, and the
# chainers return a Query of the same row type.
join_query: Query[GoodTxn] = GoodTxn.select().join(lambda t: t.account)
left_join_query: Query[GoodTxn] = GoodTxn.select().left_join(lambda t: t.account.owner)
assert_type(join_query, Query[GoodTxn])
assert_type(left_join_query, Query[GoodTxn])
