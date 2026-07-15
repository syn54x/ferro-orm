from typing import TYPE_CHECKING, Annotated, assert_type

import pytest

import ferro


class NamedSmokeMarker(ferro.Model):
    id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
    label: str


if TYPE_CHECKING:
    from ferro.models import ModelConnection
    from ferro.query import Query

    async def _typing_assertions_for_using_interface() -> None:
        """Static type-checking regression tests for `Model.using(...)`.

        This function is never executed; it exists purely so Pyright/Pylance
        can verify that every method on the `ModelConnection` returned by
        `Model.using(...)` preserves the concrete model type. If any of these
        `assert_type` calls fails under a type checker, the typing contract
        documented on `ModelConnection` has regressed.

        See `docs/plans/2026-05-07-001-refactor-generic-model-connection-plan.md`.
        """
        bound: ModelConnection[NamedSmokeMarker] = NamedSmokeMarker.using("service")
        assert_type(bound, "ModelConnection[NamedSmokeMarker]")

        assert_type(await bound.create(label="x"), NamedSmokeMarker)
        assert_type(await bound.all(), list[NamedSmokeMarker])
        assert_type(bound.select(), "Query[NamedSmokeMarker]")
        assert_type(
            bound.where(lambda t: t.id == 1),  # type: ignore[arg-type]
            "Query[NamedSmokeMarker]",
        )
        assert_type(await bound.get(1), NamedSmokeMarker)
        assert_type(await bound.get_or_none(1), NamedSmokeMarker | None)
        assert_type(await bound.bulk_create([]), int)
        assert_type(
            await bound.get_or_create(label="x"),
            tuple[NamedSmokeMarker, bool],
        )
        assert_type(
            await bound.update_or_create(defaults={"label": "y"}, label="x"),
            tuple[NamedSmokeMarker, bool],
        )


@pytest.fixture(autouse=True)
def _ensure_models_registered():
    from ferro.registry import REGISTRY

    NamedSmokeMarker._reregister_ferro()
    REGISTRY.register(NamedSmokeMarker)
    yield


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_named_connections_smoke_matrix_sqlite(tmp_path):
    """A small cross-layer smoke test for app/service route isolation."""
    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")
    await ferro.create_tables()
    await ferro.create_tables(using="service")

    async with ferro.engines.session():

        app_row = await NamedSmokeMarker.create(id=1, label="app")

        # `using="service"` conflicts with the ambient "app" session (D4)
        # unless the ambient session for that call is itself "service" —
        # a nested `engines.session("service")` block gives .using("service")
        # a matching ambient session while the outer "app" session (and its
        # identity map) stays open underneath.
        async with ferro.engines.session("service"):
            service_row = await NamedSmokeMarker.using("service").create(id=1, label="service")

        assert app_row is not service_row
        assert (await NamedSmokeMarker.get(1)).label == "app"
        async with ferro.engines.session("service"):
            assert (await NamedSmokeMarker.using("service").get(1)).label == "service"

        async with ferro.engines.session("service"):
            async with ferro.transaction(using="service") as tx:
                await tx.execute(
                    "UPDATE namedsmokemarker SET label = ? WHERE id = ?",
                    "service-tx",
                    1,
                )

        assert (await NamedSmokeMarker.get(1)).label == "app"
        async with ferro.engines.session("service"):
            raw_service = await ferro.fetch_one(
                "SELECT label FROM namedsmokemarker WHERE id = ?",
                1,
                using="service",
            )
        assert raw_service == {"label": "service-tx"}
        async with ferro.engines.session("service"):
            await service_row.refresh()
        assert service_row.label == "service-tx"

        # A raw FFI escape hatch — bypasses the Python-level route resolver
        # (and its D4 conflict check) entirely, so no session is needed. FF-D
        # D3 collapsed the tx_id/using/session_id triple into one resolved
        # RouteHandle; build it directly since this deliberately skips
        # resolve_operation_scope. The model name is the canonical identity the
        # runtime registry keys by (#249) — the same name every ORM op passes,
        # not a bare ``__name__``.
        from ferro._core import RouteHandle, delete_record

        assert (
            await delete_record(
                NamedSmokeMarker.__ferro_identity__,
                "1",
                RouteHandle(connection_name="service"),
            )
            is True
        )
        assert await NamedSmokeMarker.get(1) is app_row
        async with ferro.engines.session("service"):
            assert await NamedSmokeMarker.using("service").get_or_none(1) is None


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_named_connection_registration_and_transaction_inheritance(db_url):
    """Backend matrix smoke coverage for named registration and tx inheritance."""
    await ferro.connect(db_url, name="app", default=True)
    await ferro.connect(db_url, name="service")
    await ferro.create_tables()

    async with ferro.transaction(using="service"):
        await NamedSmokeMarker.create(id=10, label="service")

    fetched = await NamedSmokeMarker.using("service").get(10)
    assert fetched is not None
    assert fetched.label == "service"
