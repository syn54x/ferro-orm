"""``compile_query`` returns one CompiledQuery artifact (CONTEXT.md term).

The wire JSON and the hop-class map are two views of the same compile: the
map is collected from the hop facts the payload itself carries, so the two
can never disagree — the builder unpacks the artifact instead of re-walking
relation-spec chains to assemble ``hop_classes`` by hand.

The map is plan-scoped, mirroring the Rust ``needs_hop_classes`` guard
exactly (the same both-sides double-check as #272 join edges): ``None``
unless the materialization plan decodes or hydrates through a hop model's
class — an ``instances`` plan (#286) or a ``record`` plan with traversed
plain fields (#293).
"""

import json
from typing import Annotated

from ferro import BackRef, Model, Relation
from ferro.base import FerroField, ForeignKey
from ferro.query.builder import Query
from ferro.query.wire import compile_query


class CQLedger(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    accounts: Relation[list["CQAccount"]] = BackRef()


class CQAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str = ""
    ledger: Annotated[CQLedger, ForeignKey(related_name="accounts")]
    transactions: Relation[list["CQTransaction"]] = BackRef()


class CQTransaction(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    account: Annotated[CQAccount, ForeignKey(related_name="transactions")]


def test_compiled_query_carries_wire_json_payload_and_hop_classes():
    q = Query(CQTransaction).where(lambda t: t.account.label == "x")
    compiled = compile_query(q, "fetch")

    envelope = json.loads(compiled.wire_json)
    assert envelope["ir_kind"] == "query"
    assert (
        compiled.payload.to_ir_dict()["model_name"] == envelope["payload"]["model_name"]
    )
    # A root_instances plan hydrates only root rows: membership joins shape
    # the SQL but nothing decodes through a hop class, so no map travels.
    assert compiled.hop_classes is None


def test_hop_classes_none_when_nothing_traverses():
    compiled = compile_query(Query(CQTransaction), "fetch")
    assert compiled.hop_classes is None


def test_hop_classes_cover_include_paths():
    # Include paths ride the materialization plan, not the membership joins
    # (ADR-0008) — the map must cover them all the same.
    q = Query(CQTransaction).include(lambda t: t.account.ledger)
    compiled = compile_query(q, "fetch")
    assert compiled.hop_classes == {
        CQAccount.__ferro_table__: CQAccount,
        CQLedger.__ferro_table__: CQLedger,
    }


def test_hop_classes_cover_traversed_projection_paths():
    q = Query(CQTransaction).select(lambda t: t.account.label)
    compiled = compile_query(q, "fetch")
    assert compiled.hop_classes == {CQAccount.__ferro_table__: CQAccount}


def test_mutation_verbs_compile_with_no_hop_classes():
    compiled = compile_query(Query(CQTransaction), "delete")
    assert compiled.hop_classes is None
