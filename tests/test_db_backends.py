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


def test_a_test_role_is_named_after_its_schema():
    assert (
        db_backends.postgres_test_role_name("ferro_0123456789abcdef", "tenant")
        == "ferro_0123456789abcdef_tenant"
    )


def test_a_test_role_name_past_the_identifier_limit_is_refused():
    with pytest.raises(ValueError, match="63-byte identifier limit"):
        db_backends.postgres_test_role_name("ferro_0123456789abcdef", "x" * 41)


def test_server_lock_ids_are_stable_per_name_and_in_the_test_class():
    classid, objid = db_backends.server_lock_ids("pg_stat_statements")
    assert classid == db_backends.SERVER_LOCK_CLASS
    assert (classid, objid) == db_backends.server_lock_ids("pg_stat_statements")
    assert objid != db_backends.server_lock_ids("another object")[1]
    assert -(2**31) <= objid < 2**31


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
def test_the_schema_teardown_drops_the_roles_named_after_it(
    postgres_base_url, db_schema_name, pg_role
):
    """``db_url``'s teardown drops every role ``pg_role`` named, right after
    the schema, even one left owning an object and holding a grant."""
    import psycopg

    from tests.conftest import _drop_postgres_schema

    schema = f"{db_schema_name}_td"
    role = pg_role("owner")
    assert role == f"{db_schema_name}_owner"
    with psycopg.connect(postgres_base_url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'CREATE ROLE "{role}" NOSUPERUSER')
        conn.execute(f'GRANT USAGE ON SCHEMA "{db_schema_name}" TO "{role}"')
        conn.execute(f'CREATE TABLE "{schema}".owned (id int)')
        conn.execute(f'ALTER TABLE "{schema}".owned OWNER TO "{role}"')

    _drop_postgres_schema(postgres_base_url, schema, [role])

    with psycopg.connect(postgres_base_url, autocommit=True) as conn:
        left = conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchall()
    assert left == []


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
def test_pg_role_names_a_role_after_a_schema_of_the_tests_own_only(
    db_schema_name, pg_role
):
    assert pg_role("tenant", schema=f"{db_schema_name}_b") == (
        f"{db_schema_name}_b_tenant"
    )
    with pytest.raises(ValueError, match="this test's own schemas"):
        pg_role("tenant", schema="public")
