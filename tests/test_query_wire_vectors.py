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

from ferro import BackRef, FerroField, ForeignKey, ManyToMany, Model, Relation
from ferro.query.nodes import QueryProxy
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
        blob: bytes = b""
        score: int = 0
        bonus: int = 0
        tags: Relation[list["Tag"]] = BackRef()

    class Tag(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str = ""
        users: Relation[list["User"]] = ManyToMany(related_name="tags")

    # Dedicated model for the nulls= golden vector (#363) — do not hang
    # pinned_at / updated_at onto User/Account/Transaction (existing fixtures).
    class Card(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        pinned_at: str | None = None
        updated_at: str = ""

    resolve_relationships()
    return {
        "Owner": Owner,
        "Account": Account,
        "Transaction": Transaction,
        "User": User,
        "Tag": Tag,
        "Card": Card,
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


def _q_exists_bare(m: dict[str, type]) -> Any:
    # Bare 1-hop existence test on a reverse FK (#314, ADR-0007): one `exists`
    # node whose hop correlates the child's shadow FK to the root PK; the
    # inner condition tree is empty and no `joins` entry is registered.
    return m["Account"].where(lambda a: a.transactions.exists()).order_by("id")


def _q_not_exists(m: dict[str, type]) -> Any:
    # NOT EXISTS is the ordinary `not` node over an `exists` node — the
    # exists node carries no negation flag (ADR-0008 composition).
    return m["Owner"].where(lambda o: ~o.accounts.exists())


def _q_scoped_exists(m: dict[str, type]) -> Any:
    # Scoped existence test (#315): the inner lambda is a full ferro
    # predicate over the child model. The traversed inner leaf
    # (`t.account.name`) puts its hop facts on the exists node's own `joins`
    # section — rendered INSIDE the subquery, never on the root query.
    return m["Account"].where(
        lambda a: a.transactions.exists(
            lambda t: (t.amount >= 100) & (t.account.name == "checking")
        )
    )


def _q_nested_exists(m: dict[str, type]) -> Any:
    # Nested exists-in-exists (#315): the inner tree is an ordinary condition
    # tree, so depth comes from recursion, not a second mechanism. The bare
    # inner node carries no `joins` key at all (absent, not empty).
    return m["Owner"].where(
        lambda o: o.accounts.exists(lambda a: a.transactions.exists())
    )


def _q_m2m_exists(m: dict[str, type]) -> Any:
    # M2M existence test (#316): the same exists node carries a TWO-hop
    # correlation path — join table first (correlated to the enclosing
    # scope), then the target — both hops named for the one relation they
    # belong to. The scoped inner tree resolves over the target model.
    return m["User"].where(lambda u: u.tags.exists(lambda tag: tag.name == "admin"))


def _q_card_nulls(m: dict[str, type]) -> Any:
    # #363: first order_by term carries nulls=, the next omits it.
    return (
        m["Card"]
        .select()
        .where(lambda c: c.id != None)  # noqa: E711
        .order_by(lambda c: c.pinned_at, "desc", nulls="last")
        .order_by(lambda c: c.updated_at, "desc")
    )


CASES: list[tuple[str, Callable[[dict[str, type]], Any], str]] = [
    ("query_user_compound_v9", _q_user_compound, "User"),
    ("query_user_not_leaf_v9", _q_not_leaf, "User"),
    ("query_user_not_compound_v9", _q_not_compound, "User"),
    ("query_account_exists_v9", _q_exists_bare, "Account"),
    ("query_owner_not_exists_v9", _q_not_exists, "Owner"),
    ("query_account_scoped_exists_v9", _q_scoped_exists, "Account"),
    ("query_owner_nested_exists_v9", _q_nested_exists, "Owner"),
    ("query_user_m2m_exists_v9", _q_m2m_exists, "User"),
    ("query_transaction_traversal_v9", _q_traversal, "Transaction"),
    ("query_transaction_left_join_v9", _q_left_join, "Transaction"),
    ("query_transaction_include_v9", _q_include, "Transaction"),
    ("query_transaction_record_v9", _q_record, "Transaction"),
    ("query_transaction_traversed_record_v9", _q_traversed_record, "Transaction"),
    ("query_transaction_aggregate_v9", _q_aggregate, "Transaction"),
    ("query_transaction_global_aggregate_v9", _q_global_aggregate, "Transaction"),
    ("query_card_nulls_v9", _q_card_nulls, "Card"),
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


def test_literal_set_emission_matches_hand_authored_vector(
    models: dict[str, type],
) -> None:
    vector = _vector("query_user_literal_set_v9")
    expected = vector["ir"]
    assert expected["payload"]["model_name"] == "User"

    query = models["User"].where(lambda user: user.active == True)  # noqa: E712
    assignments = {
        "active": False,
        "email": "updated@ferro.dev",
        "blob": b"\x89\x00\xff",
    }
    emitted = json.loads(
        compile_query(query, "update", assignments=assignments).wire_json
    )

    expected["payload"]["model_name"] = models["User"].__ferro_identity__
    assert emitted == expected


def test_mixed_set_emission_matches_hand_authored_vector(
    models: dict[str, type],
) -> None:
    vector = _vector("query_user_mixed_set_v9")
    expected = vector["ir"]
    assert expected["payload"]["model_name"] == "User"

    query = models["User"].where(lambda user: user.active == True)  # noqa: E712
    proxy = QueryProxy(models["User"])
    emitted = json.loads(
        compile_query(
            query,
            "update",
            recipe={
                "email": "updated@ferro.dev",
                "bonus": proxy.score,
            },
        ).wire_json
    )

    expected["payload"]["model_name"] = models["User"].__ferro_identity__
    assert emitted == expected


def test_literal_set_preserves_interleaved_bytes_assignment_order(
    models: dict[str, type],
) -> None:
    payload = json.loads(
        compile_query(
            models["User"].select(),
            "update",
            assignments={
                "first_blob": b"\x01",
                "count": 2,
                "second_blob": bytearray(b"\x03"),
                "label": "four",
            },
        ).wire_json
    )["payload"]

    assert [assignment["column"] for assignment in payload["set"]] == [
        "first_blob",
        "count",
        "second_blob",
        "label",
    ]
    assert payload["set"][0]["value"]["value"] == {"kind": "bytes", "value": [1]}
    assert payload["set"][2]["value"]["value"] == {"kind": "bytes", "value": [3]}


@pytest.mark.parametrize(
    ("value", "kind", "wire_value"),
    [
        (None, "null", None),
        (42, "int", 42),
        (1.5, "float", 1.5),
        ([1, "two"], "list", [1, "two"]),
        ({"active": True}, "object", {"active": True}),
    ],
)
def test_literal_set_emits_every_json_value_kind(
    models: dict[str, type], value: object, kind: str, wire_value: object
) -> None:
    query = models["User"].select()
    payload = json.loads(
        compile_query(query, "update", assignments={"value": value}).wire_json
    )["payload"]
    assert payload["set"] == [
        {
            "column": "value",
            "value": {
                "kind": "literal",
                "value": {"kind": kind, "value": wire_value},
            },
        }
    ]


def test_envelope_is_versioned(models: dict[str, type]) -> None:
    envelope = json.loads(compile_query(models["User"].select(), "fetch").wire_json)
    assert envelope["ir_kind"] == "query"
    assert envelope["ir_version"] == 9
