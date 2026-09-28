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


def autogen_opts(extra_opts: dict | None = None) -> dict:
    """The ``context.configure(...)`` options the documented env.py recipe
    uses: type and server-default comparison, and ferro's ``render_item``,
    which is what renders a reused enum type's ``create_type=False`` (#443).
    ``extra_opts`` (``include_object``, or a ``render_item`` override for the
    unwired case) wins over the defaults."""
    from ferro.migrations import render_item

    return {
        "compare_type": True,
        "compare_server_default": True,
        "render_item": render_item,
        **(extra_opts or {}),
    }


def produce_migration_script(
    postgres_base_url: str, db_schema_name: str, *, extra_opts: dict | None = None
):
    """Autogenerate against the live per-test schema and return the
    ``MigrationScript`` (the documented env.py options, plus any
    ``extra_opts``)."""
    from alembic.autogenerate import produce_migrations
    from alembic.migration import MigrationContext

    from ferro.migrations import get_metadata

    metadata = get_metadata()
    engine = sa.create_engine(sync_url(postgres_base_url))
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'SET search_path TO "{db_schema_name}"'))
            ctx = MigrationContext.configure(conn, opts=autogen_opts(extra_opts))
            return produce_migrations(ctx, metadata)
    finally:
        engine.dispose()


def render_ops(ops, extra_opts: dict | None = None) -> str:
    """``render_python_code`` builds an ``AutogenContext`` of its own, so it
    is handed the same render hook the comparison ran under — the way
    ``alembic revision --autogenerate`` renders with env.py's options."""
    from alembic.autogenerate import render_python_code

    return render_python_code(ops, render_item=autogen_opts(extra_opts)["render_item"])


def autogen_upgrade_code(postgres_base_url: str, db_schema_name: str) -> str:
    script = produce_migration_script(postgres_base_url, db_schema_name)
    return render_ops(script.upgrade_ops)


def autogen_upgrade_and_downgrade_code(
    postgres_base_url: str, db_schema_name: str, *, extra_opts: dict | None = None
) -> tuple[str, str]:
    script = produce_migration_script(
        postgres_base_url, db_schema_name, extra_opts=extra_opts
    )
    return (
        render_ops(script.upgrade_ops, extra_opts),
        render_ops(script.downgrade_ops, extra_opts),
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
    from sqlalchemy.dialects import postgresql

    # `render_python_code` returns its lines pre-indented for splicing into a
    # revision module's `def upgrade():` body (or `downgrade`'s) — exactly
    # the shape the script.py.mako template expects. Reproduce that shape
    # literally instead of dedenting: a wrapper function, then call it.
    # The namespace carries the two imports a real revision module gets from
    # the script template: ``sa`` always, and ``postgresql`` whenever a
    # rendered type is dialect-specific (the bridge's ``render_item`` adds
    # ``from sqlalchemy.dialects import postgresql`` to the revision for a
    # reused enum type's ``create_type=False``; #443).
    module = f"def _ferro_generated():\n{code}\n"
    engine = sa.create_engine(sync_url(postgres_base_url))
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'SET search_path TO "{db_schema_name}"'))
            op = Operations(MigrationContext.configure(conn))
            namespace: dict = {"op": op, "sa": sa, "postgresql": postgresql}
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
