# ruff: noqa: E402 - the models come first, as the docs page shows them
"""Runnable companion for the Migrations loop (docs/pages/guide/schema/migrations.md).

init, new, up; then a model edit, check, new, up, drift, down, on SQLite.
"""

# --8<-- [start:models]
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    nickname: str | None = Field(default=None, index=True)  # new


# --8<-- [end:models]


from _migrations_project import project


def main() -> None:
    with project(__file__, configured=False) as app:
        app.ferro(
            "migrate", "init", "--config-file", "ferro.toml",
            "--models", "blog.models", "--dialects", "postgres,sqlite",
        )  # fmt: skip

        app.models_before()
        assert "0001_create_author/" in app.ferro("migrate", "new", "create_author")
        assert "applied" in app.ferro("migrate", "up")

        app.models_after()  # the nickname field is added
        assert "ungenerated" in app.ferro("migrate", "check", exit_code=3)
        written = app.ferro("migrate", "new", "author_nickname")
        assert "02_idx_author_nickname.up.postgres.sql" in written
        assert app.ferro("migrate", "check").startswith("ok: models match")
        app.ferro("migrate", "up")
        assert "no drift against 0002_author_nickname" in app.ferro("migrate", "drift")
        app.ferro("migrate", "status")

        assert "reverted" in app.ferro("migrate", "down", "--yes")
        assert "pending" in app.ferro("migrate", "status", exit_code=3)
        app.ferro("migrate", "up")


if __name__ == "__main__":
    main()
