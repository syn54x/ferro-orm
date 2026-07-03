"""FF-D D1: Ferro identity mapping requires weakly referenceable instances."""

import gc
import weakref

import pytest

from ferro import Field, Model
from ferro.metaclass import _assert_weakref_support


def test_model_instances_support_weakref():
    class WRUser(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    u = WRUser(id=1, name="a")
    r = weakref.ref(u)
    assert r() is u
    del u
    gc.collect()
    assert r() is None


def test_weakref_guard_rejects_unweakrefable_class():
    class NoWeakref:
        __slots__ = ()  # no __dict__, no __weakref__ anywhere in MRO

    with pytest.raises(TypeError, match="weak"):
        _assert_weakref_support(NoWeakref)


def test_weakref_guard_accepts_model_classes():
    class WRGuarded(Model):
        id: int | None = Field(default=None, primary_key=True)

    _assert_weakref_support(WRGuarded)  # must not raise
