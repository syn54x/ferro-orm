import pytest

import ferro


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_connection_error_redacts_secret_query_params(tmp_path):
    """Connection errors should give context without exposing secret query values."""
    db_file = tmp_path / "app.db"
    with pytest.raises(ferro.InterfaceError) as excinfo:
        await ferro.connect(
            f"sqlite:{db_file}?mode=rwc&password=supersecret&apikey=topsecret",
            name="app",
            default=True,
        )

    message = str(excinfo.value)
    assert "supersecret" not in message
    assert "topsecret" not in message
    assert "password=***" in message
    assert "apikey=***" in message


@pytest.mark.asyncio
async def test_connection_error_masks_an_unsupported_scheme_whole():
    """A scheme Ferro does not connect to is masked whole in the error that
    refuses it: nothing about its parse says where a secret sits."""
    with pytest.raises(ferro.InterfaceError) as excinfo:
        await ferro.connect(
            "postgres+srv://app_user:supersecret@example.invalid/app?access_token=topsecret",
            name="app",
        )

    message = str(excinfo.value)
    assert "supersecret" not in message
    assert "topsecret" not in message
    assert "app_user" not in message
    assert "DB Connection failed for postgres+srv://***: " in message
