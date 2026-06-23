"""Regression tests for framework-internal predicate style (issue #121)."""

import warnings
from typing import Annotated

import pytest

import ferro


pytestmark = pytest.mark.sqlite_only


class FrameworkPredicateMarker(ferro.Model):
    id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
    name: str


@pytest.fixture(autouse=True)
def _ensure_models_registered():
    from ferro.state import _MODEL_REGISTRY_PY

    FrameworkPredicateMarker._reregister_ferro()
    _MODEL_REGISTRY_PY[FrameworkPredicateMarker.__name__] = FrameworkPredicateMarker
    yield


def _operator_predicate_warnings(caught: list[warnings.WarningMessage]) -> list[str]:
    return [
        str(w.message)
        for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "Operator predicate style" in str(w.message)
    ]


@pytest.mark.asyncio
async def test_framework_helpers_do_not_emit_operator_predicate_warnings(tmp_path):
    db = tmp_path / "framework_predicates.db"
    await ferro.connect(f"sqlite:{db}?mode=rwc", auto_migrate=True)

    async with ferro.engines.session("default"):
        widget = FrameworkPredicateMarker(id=1, name="demo")
        await widget.save()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            loaded = await FrameworkPredicateMarker.get_or_none(1)
            assert loaded is not None
            await loaded.refresh()
            found, created = await FrameworkPredicateMarker.get_or_create(name="demo")
            assert found is not None
            assert created is False
            await loaded.delete()

        assert _operator_predicate_warnings(caught) == []


@pytest.mark.asyncio
async def test_model_connection_get_or_none_does_not_emit_operator_warnings(tmp_path):
    db = tmp_path / "framework_predicates_bound.db"
    await ferro.connect(f"sqlite:{db}?mode=rwc", auto_migrate=True)

    async with ferro.engines.session("default"):
        await FrameworkPredicateMarker.create(id=2, name="bound")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            loaded = await FrameworkPredicateMarker.using("default").get_or_none(2)

        assert loaded is not None
        assert _operator_predicate_warnings(caught) == []
