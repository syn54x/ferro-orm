import asyncio

import pytest

from ferro import version


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
async def db_engine():
    """
    Setup a clean SQLite memory database for each test.
    This is where we would eventually call our Rust 'connect' method.
    """
    db_url = "sqlite::memory:"
    # engine = await ferro.connect(db_url)
    # yield engine
    # await engine.disconnect()
    yield db_url


@pytest.fixture(autouse=True)
def cleanup_models():
    """Reset the engine between tests. Registry is not cleared so module-level
    models (e.g. in test_documentation_features) remain registered; tests that
    need a clean registry call clear_registry() in their own fixture."""
    from ferro import reset_engine

    yield
    reset_engine()
