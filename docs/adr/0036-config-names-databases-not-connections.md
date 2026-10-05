# Config names databases, not connections, and a model's database is its defining module

Amended by ADR-0039: the same keys may live in a `ferro.toml` (no `tool.ferro` prefix), every consumer reads config through `FerroSettings`, and a directory holding both files is refused. Amended by ADR-0038: the run-lock key no longer follows `tracking_schema`, and the connection-to-database binding is the `database=` and `using=` arguments of the `ferro.migrations` calls.

A project with one database configures the in-house tooling in three lines of `pyproject.toml`:

```toml
[tool.ferro]
models   = ["myapp.models"]
dialects = ["postgres", "sqlite"]
url_env  = "MYAPP_DB_DSN"        # optional; DATABASE_URL otherwise
```

A project with two names them:

```toml
[tool.ferro]
python_path = ["src"]            # optional; an uninstalled src layout

[tool.ferro.databases.app]
models   = ["myapp.models"]
dialects = ["postgres"]
url_env  = "APP_DATABASE_URL"

[tool.ferro.databases.analytics]
models   = ["myapp.analytics.models"]
dialects = ["postgres"]
url_env  = "ANALYTICS_DATABASE_URL"
tracking_schema = "ferro"        # optional; Postgres only
```

```sh
ferro migrate new add_events --database analytics   # diffs only analytics' models
ferro migrate up --database app                     # reads APP_DATABASE_URL
ferro migrate up                                    # refused: lists both names
```

Four decisions are in those files.

**A config entry names a database, not a connection.** A *database* is a named set of models whose tables live together; it has one migration lineage (one directory, one tracking table). `connect(name=…)` names a runtime route, and the two are unrelated here: an application that opens `app` read/write and `app_ro` read-only against the same server has one database in config and nothing that mentions `app_ro`. Ten tenants sharing a schema are one database run ten times with different URLs. Every command acts on exactly one database; `--database` is required when several are configured and implied when there is one. There is no `all`. Which database a given connection is checked against at `connect()` is stated explicitly by whatever does that check, never inferred from a matching name.

**A model's database is decided in config, by the module that defines it.** `models` is a list of dotted modules, imported as written for the side effect of registration; nothing is walked. A model belongs to the database whose list holds its `__module__` or a parent of it. With one database, every registered model is in it. With several, a registered model that no database claims is refused by name, a model claimed by two is allowed, and a foreign key from a model in one database to a model outside it is refused. The directory holding the config file goes on `sys.path` first, then each `python_path` entry (relative to the config file).

**The URL is never in the file, and nothing else is in the environment.** A command that needs a database takes `--url`, else the variable `url_env` names (`DATABASE_URL` by default). The whole external surface is three flags (`--config`, `--database`, `--url`) and two variables (`FERRO_CONFIG` and the URL variable). No config value has an environment or flag override, and ferro does not load `.env`.

**`dialects` is required.** ADR-0026 defaulted the target dialects to the one the generate-time URL implied. That default is gone: `migrate new` with no `dialects` refuses and prints the line to add. The generator reads no URL, so what it writes depends only on committed files.

The remaining rules:

- Config is the `[tool.ferro]` table of the nearest `pyproject.toml` that has one, walking up from the working directory. `--config <path>`, else `FERRO_CONFIG`, selects another TOML file carrying the same table. It selects and never layers.
- One database writes its keys on `[tool.ferro]`; several write `[tool.ferro.databases.<name>]`. Top-level `models`, `dialects`, `url_env`, `directory` or `tracking_schema` beside a `databases` table is refused with where to move them. Nothing inherits between entries. `python_path` is top-level in both shapes.
- `directory` (optional, per database, relative to the config file) is where the database's migrations live. It defaults to the CLI's default directory for one database and to `<default>/<database name>/` for several. Two databases resolving to the same directory, or one inside another, is refused.
- `tracking_schema` (optional, per database) moves the tracking table and its format table out of the connection's current schema on Postgres; the run-lock key follows. A missing schema refuses the run and prints the `CREATE SCHEMA` line. It is refused on a database whose `dialects` lacks `postgres`.
- `unmanaged_tables` (per database, exact names or `*` globs) is the reserved name for the list of live tables ferro leaves alone (#486). The key does not exist until that feature ships.
- An unknown key anywhere under `[tool.ferro]` is refused by name.
- Refused, each naming its fix: no `[tool.ferro]` found (the directory searched from); a listed module that fails to import (the module, the paths searched, the `python_path` line); a registry that is empty after the imports, which is never read as "drop every table"; no `--url` and the variable unset (the variable's name).

## Considered options

- **The config name is the `connect()` name.** Rejected: two connections may reach one database (a read-only role, a replica), so a connection name does not identify a lineage.
- **Membership on the class** (`__ferro_database__ = "analytics"`). Rejected: it puts a deployment fact on the model, and `using()` already makes the connection a per-query choice. Every surveyed tool keeps this mapping in config (Django routers, aerich's app → connection, Alembic's per-name metadata).
- **Every database gets every registered model.** Rejected: the registry is global, so two databases would always generate the same tables.
- **A dedicated `ferro.toml`, or a `ferro_conf.py`.** Rejected: a second home is a second place to look, and a Python config file is Alembic's `env.py` again.
- **A package to walk for models.** Rejected: it imports modules nobody named, with their side effects. No surveyed tool does it.
- **A literal `url` key, allowed for SQLite paths.** Rejected: one rule ("the file names the variable, never the value") is easier to hold than an exception by dialect.
- **Loading `.env`.** Rejected: Prisma withdrew implicit loading after clashing files; `uv run --env-file` does it on purpose.
- **Environment or flag overrides for `models`, `dialects`, `directory`, `tracking_schema`.** Rejected: these decide what a migration contains or where its record lives, so the same repository would generate different files on different machines.
- **The URL-implied dialect default** (ADR-0026). Rejected for the same reason: a developer on `sqlite:dev.db` and one on Postgres would generate different migrations from the same models.
- **A `[tool.ferro.migrations]` sub-table.** Rejected: the model list and the database grouping are read by `ferro schema dump` too, and one flat table per database reads complete on its own.
- **A `migrate up` over every database.** Rejected: one run, one lock, one database (ADR-0029).
- **Accepting `unmanaged_tables` now and ignoring it.** Rejected: a setting that silently does nothing.
- **Creating a missing `tracking_schema`.** Rejected: creating a schema is an ownership and permissions decision.
- **Dropping `python_path` for `PYTHONPATH`.** Rejected: a committed key reaches every developer and CI job; an exported variable has to be set by each.

## Consequences

- ADR-0026 is amended: target dialects are declared per database and have no default.
- ADR-0029's "a command over several database aliases" no longer exists; a command names one database.
- Changing `tracking_schema` after migrations were applied makes the database look unadopted, and `up` refuses there and names `baseline` (ADR-0031).
- The connect()-time check for pending migrations has to state which database a connection belongs to; this decision gives it no default.
- The default directory name is the CLI's call, and it must not collide with a `migrations/` that holds a project's Alembic `env.py`.
