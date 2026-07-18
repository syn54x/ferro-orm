"""Builder→wire golden-vector equality: the Python half of the QueryIR contract.

Each test builds one real query through the public chainers and asserts the
compiled wire envelope (``compile_query(...).wire_json``) equals the
hand-authored golden vector in ``tests/fixtures/ir_vectors/`` byte-for-byte.
The Rust half round-trips the same fixture files in ``crates/ferro-schema-ir``
— one artifact, both sides assert, so neither side can drift silently.

The vectors pin the wire *shape*, not this module's import path: their
``model_name`` is the bare class name, while the builder emits the
module-qualified registry identity, so the expected payload substitutes the
real identity before comparing (and asserts the bare name matches).

Vectors are hand-authored, never regenerated from builder output — an
independent authority is what makes drift detectable. When the wire
legitimately changes (the next ``ir_version`` bump), updating the vectors by
hand is the contract-review moment.

Verb policy (what ``count()`` and the mutating verbs carry) has no fetch-shaped
vector; it is pinned here against inline expected payloads instead.
"""

import json
from pathlib import Path
from typing import Annotated, Any, Callable

import pytest

from ferro import BackRef, FerroField, ForeignKey, Model, Relation
from ferro.query.wire import compile_query
from ferro.relations import resolve_relationships

VECTORS_DIR = Path(__file__).parent / "fixtures" / "ir_vectors"


def _vector(name: str) -> dict[str, Any]:
    return json.loads((VECTORS_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _build_models() -> dict[str, type]:
    """Declare the model graph the query vectors reference.

    Class names, relation field names, and shadow columns must line up with
    the vectors' hop facts (``account``/``owner`` tables, ``account_id``/
    ``owner_id`` shadow FKs), so the classes carry the vectors' bare names.
    """

    class Owner(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: str = ""
        accounts: Relation[list["Account"]] = BackRef()

    class Account(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str = ""
        balance: int = 0
        owner: Annotated[Owner | None, ForeignKey(related_name="accounts")] = None
        transactions: Relation[list["Transaction"]] = BackRef()

    class Transaction(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        amount: int = 0
        account: Annotated[Account | None, ForeignKey(related_name="transactions")] = (
            None
        )

    class User(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        active: bool = True
        email: str = ""
        role: str = ""

    resolve_relationships()
    return {
        "Owner": Owner,
        "Account": Account,
        "Transaction": Transaction,
        "User": User,
    }


@pytest.fixture()
def models(clean_registry: None) -> dict[str, type]:
    return _build_models()


# ---------------------------------------------------------------------------
# One public-chainer query per vector. Chainer order matters where the vector
# pins join insertion order (the ``joins`` list is insertion-ordered).
# ---------------------------------------------------------------------------


def _q_user_compound(m: dict[str, type]) -> Any:
    return (
        m["User"]
        .where(
            lambda u: (
                (u.active == True)  # noqa: E712
                & ((u.email.like("%@ferro.dev")) | (u.role.in_(["admin", "owner"])))
            )
        )
        .order_by("id")
        .limit(100)
        .offset(0)
    )


def _q_traversal(m: dict[str, type]) -> Any:
    # order_by first so the ("account",) join registers before the where
    # clause's ("account", "owner") — the vector pins that insertion order.
    return (
        m["Transaction"]
        .select()
        .order_by(lambda t: t.account.name)
        .order_by("id")
        .where(
            lambda t: (t.account.owner.email == "owner@ferro.dev") & (t.amount >= 100)
        )
        .limit(50)
        .offset(0)
    )


def _q_left_join(m: dict[str, type]) -> Any:
    return (
        m["Transaction"]
        .select()
        .left_join(lambda t: t.account)
        .where(lambda t: t.account.owner.email == "owner@ferro.dev")
    )


def _q_include(m: dict[str, type]) -> Any:
    return (
        m["Transaction"]
        .where(lambda t: t.amount >= 100)
        .order_by("id")
        .include(lambda t: t.account)
    )


def _q_record(m: dict[str, type]) -> Any:
    return (
        m["Transaction"]
        .where(lambda t: t.amount >= 100)
        .select(lambda t: (t.id, t.amount))
        .order_by("amount", "desc")
        .limit(25)
    )


def _q_traversed_record(m: dict[str, type]) -> Any:
    return (
        m["Transaction"]
        .select()
        .left_join(lambda t: t.account)
        .where(lambda t: t.amount >= 100)
        .select(
            lambda t: {
                "txn_id": t.id,
                "account_name": t.account.name,
                "owner_email": t.account.owner.email,
            }
        )
        .order_by("id")
    )


def _q_aggregate(m: dict[str, type]) -> Any:
    return (
        m["Transaction"]
        .where(lambda t: t.amount >= 100)
        .select(
            lambda t: {
                "account_name": t.account.name,
                "total": t.amount.sum(),
                "avg_balance": t.account.balance.avg(),
            }
        )
        .order_by("total", "desc")
        .limit(5)
    )


def _q_global_aggregate(m: dict[str, type]) -> Any:
    return (
        m["Transaction"]
        .where(lambda t: t.amount >= 100)
        .select(
            lambda t: {
                "n": t.id.count(),
                "total": t.amount.sum(),
                "avg_balance": t.account.balance.avg(),
            }
        )
    )


def _q_not_leaf(m: dict[str, type]) -> Any:
    return (
        m["User"]
        .where(lambda u: ~u.role.in_(["admin", "owner"]))
        .order_by("id")
        .limit(100)
        .offset(0)
    )


def _q_not_compound(m: dict[str, type]) -> Any:
    return m["User"].where(
        lambda u: ~((u.active == True) | (u.email.like("%@ferro.dev")))  # noqa: E712
    )


CASES: list[tuple[str, Callable[[dict[str, type]], Any], str]] = [
    ("query_user_compound_v6", _q_user_compound, "User"),
    ("query_user_not_leaf_v6", _q_not_leaf, "User"),
    ("query_user_not_compound_v6", _q_not_compound, "User"),
    ("query_transaction_traversal_v6", _q_traversal, "Transaction"),
    ("query_transaction_left_join_v6", _q_left_join, "Transaction"),
    ("query_transaction_include_v6", _q_include, "Transaction"),
    ("query_transaction_record_v6", _q_record, "Transaction"),
    ("query_transaction_traversed_record_v6", _q_traversed_record, "Transaction"),
    ("query_transaction_aggregate_v6", _q_aggregate, "Transaction"),
    ("query_transaction_global_aggregate_v6", _q_global_aggregate, "Transaction"),
]


@pytest.mark.parametrize(
    ("vector_name", "build", "root"), CASES, ids=[case[0] for case in CASES]
)
def test_builder_emission_matches_vector(
    models: dict[str, type],
    vector_name: str,
    build: Callable[[dict[str, type]], Any],
    root: str,
) -> None:
    vector = _vector(vector_name)
    expected = vector["ir"]
    assert expected["payload"]["model_name"] == root

    emitted = json.loads(compile_query(build(models), "fetch").wire_json)

    expected["payload"]["model_name"] = models[root].__ferro_identity__
    assert emitted == expected


# ---------------------------------------------------------------------------
# Verb policy: what count() and the mutating verbs put on the wire.
# ---------------------------------------------------------------------------


def _payload(query: Any, verb: str) -> dict[str, Any]:
    return json.loads(compile_query(query, verb).wire_json)["payload"]


def test_count_zeroes_ordering_and_paging_but_keeps_joins(
    models: dict[str, type],
) -> None:
    query = (
        models["Transaction"]
        .where(lambda t: t.account.name == "checking")
        .order_by("id")
        .limit(3)
        .offset(1)
    )
    payload = _payload(query, "count")
    assert payload["order_by"] == []
    assert payload["limit"] is None
    assert payload["offset"] is None
    assert len(payload["joins"]) == 1
    assert payload["materialization"] == {"kind": "root_instances"}


def test_count_on_a_projection_stays_root_instances(
    models: dict[str, type],
) -> None:
    # count() is projection-blind (PRD #277 verb table): it materializes a
    # scalar, so the plan is root_instances even on a projected query.
    query = (
        models["Transaction"]
        .where(lambda t: t.amount >= 100)
        .select(lambda t: (t.id, t.amount))
    )
    assert _payload(query, "count")["materialization"] == {"kind": "root_instances"}


def test_mutate_payload_omits_pagination_keys(models: dict[str, type]) -> None:
    # Mutating payloads carry no limit/offset keys at all (absent, not null):
    # portable SQL has no UPDATE/DELETE ... LIMIT, and pagination is rejected
    # before compilation — the absent keys are the pinned wire bytes.
    query = models["User"].where(lambda u: u.active == True)  # noqa: E712
    for verb in ("update", "delete"):
        payload = _payload(query, verb)
        assert "limit" not in payload
        assert "offset" not in payload
        assert payload["order_by"] == []
        assert payload["m2m"] is None
        assert payload["joins"] == []
        assert payload["materialization"] == {"kind": "root_instances"}


def test_envelope_is_versioned(models: dict[str, type]) -> None:
    envelope = json.loads(compile_query(models["User"].select(), "fetch").wire_json)
    assert envelope["ir_kind"] == "query"
    assert envelope["ir_version"] == 6
