from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse


SUPPORTED_BACKENDS = ("sqlite", "postgres")
LOCAL_POSTGRES_PROVIDER = "local"


def load_env_value(env_file: Path, key: str) -> str | None:
    if not env_file.exists():
        return None

    prefix = f"{key}="
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue

        value = line[len(prefix) :].strip()
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        return value

    return None


def get_postgres_url(env: dict[str, str], env_file: Path) -> str | None:
    """Return an externally managed Postgres URL, if one is configured."""
    if env.get("FERRO_POSTGRES_PROVIDER") == LOCAL_POSTGRES_PROVIDER:
        return None

    return (
        env.get("FERRO_POSTGRES_URL")
        or load_env_value(env_file, "FERRO_POSTGRES_URL")
        or env.get("FERRO_SUPABASE_URL")
        or load_env_value(env_file, "FERRO_SUPABASE_URL")
    )


def get_supabase_url(env: dict[str, str], env_file: Path) -> str | None:
    """Backward-compatible alias for the old Supabase-only test setting."""
    return get_postgres_url(env, env_file)


def has_pytest_postgresql() -> bool:
    return importlib.util.find_spec("pytest_postgresql") is not None


def parse_backend_option(raw_value: str) -> tuple[str, ...]:
    backends = tuple(part.strip() for part in raw_value.split(",") if part.strip())
    invalid = sorted(set(backends) - set(SUPPORTED_BACKENDS))
    if invalid:
        joined = ", ".join(invalid)
        raise ValueError(f"Unsupported database backend: {joined}")
    return backends or ("sqlite",)


def backends_for_test(
    selected_backends: tuple[str, ...],
    *,
    is_backend_matrix: bool,
    is_sqlite_only: bool,
    is_postgres_only: bool,
    has_postgres_url: bool,
) -> tuple[str, ...]:
    if is_sqlite_only:
        return ("sqlite",)

    if is_postgres_only:
        return ("postgres",) if has_postgres_url and "postgres" in selected_backends else ()

    if is_backend_matrix:
        if has_postgres_url:
            return selected_backends
        return tuple(backend for backend in selected_backends if backend != "postgres")

    return ("sqlite",)


def build_postgres_test_url(base_url: str, schema_name: str) -> str:
    parsed = urlparse(base_url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    params.append(("ferro_search_path", schema_name))
    return urlunparse(parsed._replace(query=urlencode(params)))


def build_postgres_url_from_connection_params(params: dict[str, str]) -> str:
    dbname = params.get("dbname") or params.get("database") or "postgres"
    host = params.get("host") or "localhost"
    port = params.get("port")
    user = params.get("user")
    password = params.get("password")

    userinfo = ""
    if user:
        userinfo = quote(user, safe="")
        if password:
            userinfo += f":{quote(password, safe='')}"
        userinfo += "@"

    # libpq can report a Unix socket path as host; keep the URL TCP-shaped and
    # pass the socket through the query string for psycopg/sqlx compatibility.
    query = ""
    if host.startswith("/"):
        query = urlencode({"host": host})
        host = "localhost"

    netloc = f"{userinfo}{host}"
    if port:
        netloc += f":{port}"

    return urlunparse(("postgresql", netloc, f"/{quote(dbname, safe='')}", "", query, ""))
