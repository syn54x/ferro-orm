import pytest

import ferro


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_connection_error_redacts_secret_query_params(tmp_path):
    """Connection errors should give context without exposing secret query values."""
    db_file = tmp_path / "app.db"
    with pytest.raises(ConnectionError) as excinfo:
        await ferro.connect(
            f"sqlite:{db_file}?mode=rwc&password=supersecret&apikey=topsecret",
            name="app",
            default=True,
        )

    message = str(excinfo.value)
    assert "supersecret" not in message
    assert "topsecret" not in message
    assert "password=<redacted>" in message
    assert "apikey=<redacted>" in message


@pytest.mark.asyncio
async def test_connection_error_redacts_postgres_userinfo_and_tokens():
    """Postgres-style DSNs should redact password userinfo and token query params."""
    with pytest.raises(ConnectionError) as excinfo:
        await ferro.connect(
            "postgres+srv://app_user:supersecret@example.invalid/app?access_token=topsecret",
            name="app",
        )

    message = str(excinfo.value)
    assert "supersecret" not in message
    assert "topsecret" not in message
    assert "app_user:<redacted>@" in message
    assert "access_token=<redacted>" in message
