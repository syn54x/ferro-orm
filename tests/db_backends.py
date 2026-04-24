from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


SUPPORTED_BACKENDS = ("sqlite", "postgres")


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


def get_supabase_url(env: dict[str, str], env_file: Path) -> str | None:
    return env.get("FERRO_SUPABASE_URL") or load_env_value(env_file, "FERRO_SUPABASE_URL")


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
