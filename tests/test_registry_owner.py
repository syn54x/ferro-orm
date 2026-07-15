"""The Registry owns every Python-side registration store and their agreement.

One module-level instance (``REGISTRY``) holds the model registry, envelope +
fingerprint caches, join-table bundles, pending relations, the modelset
artifact, and the generation counters. The agreement invariants — fingerprint
is a pure function of its envelope, join-table eviction clears envelopes too,
a test reset wipes everything — are the Registry's locality, not a convention
spread across callers and fixtures.
"""

from ferro.registry import REGISTRY, ir_fingerprint


class _FakeModel:
    __ferro_identity__ = "tests.fake.RegFake"
    __name__ = "RegFake"
    __qualname__ = "RegFake"
    __ferro_table__ = "regfake"


def test_register_and_deregister_round_trip_bumps_generation():
    snap = REGISTRY.snapshot()
    try:
        before = REGISTRY.registration_generation()
        REGISTRY.register(_FakeModel)
        assert REGISTRY.models()["tests.fake.RegFake"] is _FakeModel
        assert REGISTRY.registration_generation() == before + 1
        assert REGISTRY.is_dirty()

        REGISTRY.deregister("tests.fake.RegFake")
        assert "tests.fake.RegFake" not in REGISTRY.models()
        assert REGISTRY.registration_generation() == before + 2
    finally:
        REGISTRY.restore(snap)


def test_persist_envelope_pairs_fingerprint():
    snap = REGISTRY.snapshot()
    try:
        envelope = {"ir_kind": "schema", "payload": {"model_name": "X"}}
        REGISTRY.persist_envelope("tests.fake.RegFake", envelope)
        assert REGISTRY.envelope("tests.fake.RegFake") is envelope
        assert REGISTRY.fingerprint("tests.fake.RegFake") == ir_fingerprint(envelope)

        REGISTRY.evict_envelope("tests.fake.RegFake")
        assert REGISTRY.envelope("tests.fake.RegFake") is None
        assert REGISTRY.fingerprint("tests.fake.RegFake") is None
    finally:
        REGISTRY.restore(snap)


def test_missing_envelope_names_is_store_arithmetic():
    snap = REGISTRY.snapshot()
    try:
        REGISTRY.register(_FakeModel)
        REGISTRY.evict_envelope("tests.fake.RegFake")
        assert "tests.fake.RegFake" in REGISTRY.missing_envelope_names()

        REGISTRY.persist_envelope("tests.fake.RegFake", {"payload": {}})
        assert "tests.fake.RegFake" not in REGISTRY.missing_envelope_names()
    finally:
        REGISTRY.restore(snap)


def test_clear_join_tables_evicts_their_envelopes_too():
    snap = REGISTRY.snapshot()
    try:
        REGISTRY.add_join_table("jt_a_b", {"columns": []})
        REGISTRY.persist_envelope("jt_a_b", {"payload": {"model_name": "jt_a_b"}})
        REGISTRY.clear_join_tables()
        assert "jt_a_b" not in REGISTRY.join_tables()
        assert REGISTRY.envelope("jt_a_b") is None
    finally:
        REGISTRY.restore(snap)


def test_snapshot_restore_round_trips_every_store():
    snap = REGISTRY.snapshot()
    try:
        REGISTRY.register(_FakeModel)
        REGISTRY.defer_relation("tests.fake.RegFake", "other", object())
        REGISTRY.add_join_table("jt_x_y", {"columns": []})
        REGISTRY.set_modelset({"payload": {"models": []}})
        assert REGISTRY.is_dirty()
    finally:
        REGISTRY.restore(snap)
    assert "tests.fake.RegFake" not in REGISTRY.models()
    assert "jt_x_y" not in REGISTRY.join_tables()


def test_set_modelset_pairs_fingerprint():
    snap = REGISTRY.snapshot()
    try:
        envelope = {"payload": {"models": []}}
        REGISTRY.set_modelset(envelope)
        stored, fingerprint = REGISTRY.modelset()
        assert stored is envelope
        assert fingerprint == ir_fingerprint(envelope)
    finally:
        REGISTRY.restore(snap)


def test_pending_relations_drain_and_dirty():
    snap = REGISTRY.snapshot()
    try:
        # Earlier-collected modules may have left deferred relations behind;
        # drain to a known-clean baseline (restored by the snapshot below).
        REGISTRY.drain_pending_relations()
        REGISTRY.mark_resolved()
        assert not REGISTRY.is_dirty()
        REGISTRY.defer_relation("tests.fake.RegFake", "other", object())
        assert REGISTRY.is_dirty()

        drained = REGISTRY.drain_pending_relations()
        assert len(drained) == 1
        assert drained[0][0] == "tests.fake.RegFake"
        assert REGISTRY.drain_pending_relations() == []
    finally:
        REGISTRY.restore(snap)
