from pathlib import Path

import pytest

from tests import db_backends


def test_load_env_value_reads_quoted_values(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'IGNORED_KEY="ignore me"\nFERRO_SUPABASE_URL="postgresql://user:pass@db.supabase.co/postgres?sslmode=require"\n',
        encoding="utf-8",
    )

    assert (
        db_backends.load_env_value(env_file, "FERRO_SUPABASE_URL")
        == "postgresql://user:pass@db.supabase.co/postgres?sslmode=require"
    )


def test_get_supabase_url_prefers_environment_over_dotenv(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FERRO_SUPABASE_URL=postgresql://dotenv.example/postgres\n",
        encoding="utf-8",
    )

    assert (
        db_backends.get_supabase_url(
            {"FERRO_SUPABASE_URL": "postgresql://env.example/postgres"},
            env_file,
        )
        == "postgresql://env.example/postgres"
    )


def test_get_postgres_url_prefers_generic_setting_over_supabase(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FERRO_SUPABASE_URL=postgresql://dotenv-supabase.example/postgres\n",
        encoding="utf-8",
    )

    assert (
        db_backends.get_postgres_url(
            {
                "FERRO_POSTGRES_URL": "postgresql://generic.example/postgres",
                "FERRO_SUPABASE_URL": "postgresql://env-supabase.example/postgres",
            },
            env_file,
        )
        == "postgresql://generic.example/postgres"
    )


def test_get_postgres_url_can_force_local_provider(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FERRO_POSTGRES_URL=postgresql://dotenv.example/postgres\n",
        encoding="utf-8",
    )

    assert (
        db_backends.get_postgres_url(
            {"FERRO_POSTGRES_PROVIDER": "local"},
            env_file,
        )
        is None
    )


def test_parse_backend_option_validates_backend_names():
    assert db_backends.parse_backend_option("sqlite,postgres") == ("sqlite", "postgres")

    with pytest.raises(ValueError, match="Unsupported database backend"):
        db_backends.parse_backend_option("sqlite,mysql")


def test_backends_for_test_respects_markers_and_available_postgres():
    assert db_backends.backends_for_test(
        ("sqlite", "postgres"),
        is_backend_matrix=True,
        is_sqlite_only=False,
        is_postgres_only=False,
        has_postgres_url=True,
    ) == ("sqlite", "postgres")

    assert db_backends.backends_for_test(
        ("sqlite", "postgres"),
        is_backend_matrix=False,
        is_sqlite_only=True,
        is_postgres_only=False,
        has_postgres_url=True,
    ) == ("sqlite",)

    assert db_backends.backends_for_test(
        ("sqlite", "postgres"),
        is_backend_matrix=False,
        is_sqlite_only=False,
        is_postgres_only=True,
        has_postgres_url=False,
    ) == ()


def test_build_postgres_test_url_sets_search_path():
    url = db_backends.build_postgres_test_url(
        "postgresql://user:pass@db.supabase.co/postgres?sslmode=require",
        "ferro_test_schema",
    )

    assert "sslmode=require" in url
    assert "ferro_search_path=ferro_test_schema" in url


def test_build_postgres_url_from_connection_params():
    url = db_backends.build_postgres_url_from_connection_params(
        {
            "host": "127.0.0.1",
            "port": "55432",
            "user": "postgres",
            "password": "secret value",
            "dbname": "test_db",
        }
    )

    assert url == "postgresql://postgres:secret%20value@127.0.0.1:55432/test_db"
