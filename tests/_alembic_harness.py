"""Live Alembic autogenerate harness shared by the comparator suites.

Runs real ``produce_migrations`` against the per-test Postgres schema and
executes the rendered ``upgrade()`` / ``downgrade()`` code exactly as a
revision module would. One home for what ``test_label_addition``,
``test_table_check_*``, ``test_row_security_alembic`` and
``test_alembic_enum_type_drop`` all need.
"""

from __future__ import annotations

import sqlalchemy as sa


def sync_url(postgres_base_url: str) -> str:
    """The synchronous SQLAlchemy URL for ferro's Postgres URL."""
    for scheme in ("postgresql://", "postgres://"):
        if postgres_base_url.startswith(scheme):
            return "postgresql+psycopg://" + postgres_base_url[len(scheme) :]
    return postgres_base_url


def produce_migration_script(
    postgres_base_url: str, db_schema_name: str, *, extra_opts: dict | None = None
):
    """Autogenerate against the live per-test schema and return the
    ``MigrationScript`` (``compare_type`` and ``compare_server_default`` on,
    plus any ``extra_opts`` such as ``include_object``)."""
    from alembic.autogenerate import produce_migrations
    from alembic.migration import MigrationContext

    from ferro.migrations import get_metadata

    metadata = get_metadata()
    engine = sa.create_engine(sync_url(postgres_base_url))
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'SET search_path TO "{db_schema_name}"'))
            opts = {"compare_type": True, "compare_server_default": True}
            ctx = MigrationContext.configure(conn, opts={**opts, **(extra_opts or {})})
            return produce_migrations(ctx, metadata)
    finally:
        engine.dispose()


def autogen_upgrade_code(postgres_base_url: str, db_schema_name: str) -> str:
    from alembic.autogenerate import render_python_code

    script = produce_migration_script(postgres_base_url, db_schema_name)
    return render_python_code(script.upgrade_ops)


def autogen_upgrade_and_downgrade_code(
    postgres_base_url: str, db_schema_name: str, *, extra_opts: dict | None = None
) -> tuple[str, str]:
    from alembic.autogenerate import render_python_code

    script = produce_migration_script(
        postgres_base_url, db_schema_name, extra_opts=extra_opts
    )
    return (
        render_python_code(script.upgrade_ops),
        render_python_code(script.downgrade_ops),
    )


def run_generated_code(code: str, postgres_base_url: str, db_schema_name: str) -> None:
    """Execute one side (upgrade or downgrade) of a generated revision's code
    against the live database, exactly as a real revision module's own
    ``upgrade()``/``downgrade()`` would — no temp file needed since
    ``render_python_code`` returns bare, already-runnable statements bound to
    a plain ``Operations`` instance (the checks/enum family's ops only ever
    render ``op.execute``/``op.drop_constraint`` calls, both real
    ``Operations`` methods)."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    # `render_python_code` returns its lines pre-indented for splicing into a
    # revision module's `def upgrade():` body (or `downgrade`'s) — exactly
    # the shape the script.py.mako template expects. Reproduce that shape
    # literally instead of dedenting: a wrapper function, then call it.
    module = f"def _ferro_generated():\n{code}\n"
    engine = sa.create_engine(sync_url(postgres_base_url))
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'SET search_path TO "{db_schema_name}"'))
            op = Operations(MigrationContext.configure(conn))
            namespace: dict = {"op": op, "sa": sa}
            exec(compile(module, "<generated-revision>", "exec"), namespace)
            namespace["_ferro_generated"]()
            conn.commit()
    finally:
        engine.dispose()


def assert_statement_in_code(statement: str, code: str) -> None:
    """``render_python_code`` embeds every statement as a Python string
    literal (``op.execute('...')``); comparing against ``repr(statement)``
    (rather than the raw SQL) is what makes this survive statements that
    themselves contain single-quoted SQL literals, e.g. the shorthand's
    ``current_setting('pinch.ledger_id', true)``."""
    assert repr(statement) in code, (statement, code)
