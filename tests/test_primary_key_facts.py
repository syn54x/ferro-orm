"""The PK fact is derived once at class-body compile and cached on the class.

``__ferro_pk__`` is set at the compile choke point (the same site that attaches
``__ferro_columns__``), so every consumer — save, descriptors, shadow-FK typing,
traversal — reads one cached fact instead of re-looping the column specs.
At most one ``primary_key=True`` column is legal; the violation raises at class
definition time (ADR-0002's flagged follow-up). Zero PKs stays a legal
declaration: ``__ferro_pk__`` is ``None`` and the operations that genuinely
need a PK raise clear errors instead of guessing (the old silent ``"id"``
fallback).
"""

from typing import Annotated

import pytest

from ferro import Field, Model
from ferro.base import ForeignKey


@pytest.fixture(autouse=True)
def _isolate_relation_state():
    """Restore the global relation registries after each test.

    Same pattern as ``test_fk_target_pk.py``: models declared inside test
    functions still register globally at class-creation time, so snapshot and
    restore the registry and pending-relations stores around each test.
    """
    from ferro.registry import REGISTRY

    snapshot = REGISTRY.snapshot()
    yield
    REGISTRY.restore(snapshot)


def test_pk_field_name_cached_on_class():
    class PkCachedDefault(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class PkCachedCustom(Model):
        code: str = Field(primary_key=True)
        city: str

    assert PkCachedDefault.__ferro_pk__ == "id"
    assert PkCachedCustom.__ferro_pk__ == "code"


def test_zero_pk_model_caches_none():
    class PkLessLog(Model):
        message: str
        level: int

    assert PkLessLog.__ferro_pk__ is None


def test_multiple_primary_keys_rejected_at_class_definition():
    # TypeError is the declaration-error contract: the metaclass surfaces it
    # unwrapped rather than disguising it as a registration RuntimeError.
    with pytest.raises(TypeError, match=r"PkTwice.*'code'.*'id'"):

        class PkTwice(Model):
            id: int | None = Field(default=None, primary_key=True)
            code: str = Field(primary_key=True)


def test_fk_assignment_to_pkless_target_raises_instead_of_guessing_id():
    class PkLessTarget(Model):
        label: str

    class PkGuessReferrer(Model):
        id: int | None = Field(default=None, primary_key=True)
        target: Annotated[PkLessTarget, ForeignKey(related_name="referrers")]

    target = PkLessTarget(label="orphan")
    with pytest.raises(ValueError, match=r"PkLessTarget.*no primary-key column"):
        PkGuessReferrer(target=target)
