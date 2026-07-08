import asyncio
import os
import uuid
from pathlib import Path

import pytest

from ferro import version
from tests.db_backends import (
    backends_for_test,
    build_postgres_url_from_connection_params,
    build_postgres_test_url,
    get_postgres_url,
    has_pytest_postgresql,
    parse_backend_option,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT_DIR / ".env"


def pytest_addoption(parser):
    parser.addoption(
        "--db-backends",
        action="store",
        default="sqlite",
        help="Comma-separated database backends to use for backend_matrix tests: sqlite,postgres.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "backend_matrix: run this test against each selected database backend.",
    )
    config.addinivalue_line(
        "markers",
        "sqlite_only: run this test only against SQLite.",
    )
    config.addinivalue_line(
        "markers",
        "postgres_only: run this test only against Postgres.",
    )


def _selected_backends(config: pytest.Config) -> tuple[str, ...]:
    try:
        return parse_backend_option(config.getoption("--db-backends"))
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc


def _available_postgres_url() -> str | None:
    return get_postgres_url(dict(os.environ), ENV_FILE)


def _has_postgres_provider() -> bool:
    return bool(_available_postgres_url()) or has_pytest_postgresql()


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "db_url" not in metafunc.fixturenames:
        return

    selected_backends = _selected_backends(metafunc.config)
    test_backends = backends_for_test(
        selected_backends,
        is_backend_matrix=metafunc.definition.get_closest_marker("backend_matrix")
        is not None,
        is_sqlite_only=metafunc.definition.get_closest_marker("sqlite_only") is not None,
        is_postgres_only=metafunc.definition.get_closest_marker("postgres_only") is not None,
        has_postgres_url=_has_postgres_provider(),
    )

    if not test_backends:
        metafunc.parametrize(
            "db_url",
            [
                pytest.param(
                    None,
                    marks=pytest.mark.skip(
                        reason="No Postgres provider is configured for Postgres-backed tests.",
                    ),
                )
            ],
            indirect=True,
            ids=["postgres"],
        )
        return

    metafunc.parametrize("db_url", test_backends, indirect=True, ids=test_backends)


def _connect_postgres_admin(base_url: str):
    import psycopg

    return psycopg.connect(base_url, autocommit=True)


def _create_postgres_schema(base_url: str, schema_name: str) -> None:
    with _connect_postgres_admin(base_url) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        conn.execute(f'CREATE SCHEMA "{schema_name}"')


def _drop_postgres_schema(base_url: str, schema_name: str) -> None:
    with _connect_postgres_admin(base_url) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')


def _pytest_postgresql_base_url(request: pytest.FixtureRequest) -> str:
    try:
        conn = request.getfixturevalue("postgresql")
    except Exception as exc:
        pytest.skip(
            "pytest-postgresql could not start local Postgres. "
            "Install Postgres server binaries or set FERRO_POSTGRES_URL. "
            f"Original error: {exc}"
        )

    return build_postgres_url_from_connection_params(conn.info.get_parameters())


# This fixture ensures the Rust binary is actually loaded and working
@pytest.fixture(scope="session", autouse=True)
def check_engine():
    """Verify that the Rust binary is compiled and accessible."""
    try:
        v = version()
        print(f"\n✅ Ferro Engine Verified: {v}")
    except ImportError:
        pytest.fail("Ferro binary not found. Run 'uv run maturin develop' first.")


@pytest.fixture(scope="session")
def event_loop():
    """Create a persistent event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="function")
def db_url(request: pytest.FixtureRequest, tmp_path: Path):
    backend = getattr(request, "param", "sqlite")

    if backend == "sqlite":
        request.node._ferro_db_schema = None
        db_file = tmp_path / f"{request.node.name}.db"
        yield f"sqlite:{db_file}?mode=rwc"
        return

    base_url = _available_postgres_url() or _pytest_postgresql_base_url(request)
    if not base_url:
        pytest.skip("No Postgres provider is configured for Postgres-backed tests.")

    schema_name = f"ferro_{uuid.uuid4().hex[:16]}"
    _create_postgres_schema(base_url, schema_name)
    request.node._ferro_db_schema = schema_name
    request.node._ferro_postgres_base_url = base_url

    try:
        yield build_postgres_test_url(base_url, schema_name)
    finally:
        from ferro import reset_engine

        reset_engine()
        _drop_postgres_schema(base_url, schema_name)


@pytest.fixture(scope="function")
def postgres_base_url(request: pytest.FixtureRequest, db_url: str | None) -> str | None:
    if db_url is None or not (
        db_url.startswith("postgres://") or db_url.startswith("postgresql://")
    ):
        return None
    return getattr(request.node, "_ferro_postgres_base_url", _available_postgres_url())


@pytest.fixture(scope="function")
def db_schema_name(request: pytest.FixtureRequest, db_url: str | None) -> str | None:
    if db_url is None or not (
        db_url.startswith("postgres://") or db_url.startswith("postgresql://")
    ):
        return None
    return getattr(request.node, "_ferro_db_schema", None)


@pytest.fixture(scope="function")
def db_backend(db_url: str) -> str:
    if db_url.startswith("postgres://") or db_url.startswith("postgresql://"):
        return "postgres"
    return "sqlite"


@pytest.fixture(scope="function")
async def db_engine():
    """Compatibility fixture for older tests that only need a SQLite URL."""
    yield "sqlite::memory:"


@pytest.fixture(autouse=True)
def cleanup_models():
    """Reset the engine between tests. Registry is not cleared so module-level
    models (e.g. in test_documentation_features) remain registered; tests that
    need a clean registry call clear_registry() in their own fixture."""
    from ferro import reset_engine

    yield
    reset_engine()


@pytest.fixture()
def clean_registry():
    """Fully wipe every per-model store before and after the test.

    Shared home for the registry-wipe pattern that several suites previously
    hand-rolled (e.g. ``test_ir_vectors_contract``'s ``clean_model_registry``,
    the ``test_registry_entrypoints`` guard). Threading a newly added store into
    the reset now happens once here instead of in each copy.
    """
    from ferro import clear_registry, reset_engine
    from ferro import state as ferro_state

    def _wipe() -> None:
        reset_engine()
        clear_registry()
        ferro_state._MODEL_REGISTRY_PY.clear()
        ferro_state._PENDING_RELATIONS.clear()
        ferro_state._JOIN_TABLE_REGISTRY.clear()
        ferro_state._SCHEMA_IR_BY_MODEL.clear()
        ferro_state._SCHEMA_IR_FINGERPRINT_BY_MODEL.clear()
        ferro_state._SCHEMA_IR_MODELSET = None
        ferro_state._SCHEMA_IR_MODELSET_FINGERPRINT = None

    _wipe()
    yield
    _wipe()


@pytest.fixture(autouse=True)
def _ferro_registry_isolation():
    """Snapshot/restore the global model registries around every test (FF-E).

    Function-local test models used to accumulate in the global registries
    for the whole session — harmless under bare-class-name keys (later
    same-named models clobbered earlier ones) but fatal under FF-E's
    qualified keys + table-name collision detection. Module-scope models are
    captured in the baseline snapshot and survive; function-local models are
    dropped when the test ends.
    """
    from ferro.state import (
        _JOIN_TABLE_REGISTRY,
        _MODEL_REGISTRY_PY,
        _PENDING_RELATIONS,
        deregister_model,
    )

    models_snapshot = dict(_MODEL_REGISTRY_PY)
    pending_snapshot = list(_PENDING_RELATIONS)
    joins_snapshot = dict(_JOIN_TABLE_REGISTRY)
    yield
    # Deregister function-local models through the entrypoint so their envelope
    # + fingerprint are evicted too — a bare `.clear()`/restore of the registry
    # would leave those caches disagreeing (the divergence #243 eliminates).
    for name in set(_MODEL_REGISTRY_PY) - set(models_snapshot):
        deregister_model(name)
    _MODEL_REGISTRY_PY.clear()
    _MODEL_REGISTRY_PY.update(models_snapshot)
    _PENDING_RELATIONS.clear()
    _PENDING_RELATIONS.extend(pending_snapshot)
    _JOIN_TABLE_REGISTRY.clear()
    _JOIN_TABLE_REGISTRY.update(joins_snapshot)
