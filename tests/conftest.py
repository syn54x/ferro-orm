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
