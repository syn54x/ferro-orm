# Configuration

A project commits one file that tells Ferro's tooling where its models are and which dialects its migrations target. `ferro migrate init` writes it; every `ferro migrate` verb, `ferro.migrations.*`, the Alembic bridge's `get_metadata()`, and `connect()` with an auto-migrate flag read it through one class, `ferro.FerroSettings`.

## The two file forms

The same keys, at the top level of a `ferro.toml` or under `[tool.ferro]` in `pyproject.toml`:

=== "ferro.toml"

    ```toml
    models = ["blog.models"]
    dialects = ["postgres", "sqlite"]
    ```

=== "pyproject.toml"

    ```toml
    [tool.ferro]
    models = ["blog.models"]
    dialects = ["postgres", "sqlite"]
    ```

A project has one or the other, never both: a directory holding a `ferro.toml` and a `pyproject.toml` with `[tool.ferro]` is refused, and so is a file that carries ferro keys both at its top level and under `[tool.ferro]`. Ferro reads one file and never merges two.

### Several databases

A project whose models live in separate databases, each with its own migration history, declares one table per database. Nothing is inherited between them:

=== "ferro.toml"

    ```toml
    [databases.main]
    models = ["blog.models"]
    dialects = ["postgres", "sqlite"]
    url_env = "MAIN_DATABASE_URL"

    [databases.billing]
    models = ["billing.models"]
    dialects = ["postgres"]
    url_env = "BILLING_DATABASE_URL"
    ```

=== "pyproject.toml"

    ```toml
    [tool.ferro.databases.main]
    models = ["blog.models"]
    dialects = ["postgres", "sqlite"]
    url_env = "MAIN_DATABASE_URL"

    [tool.ferro.databases.billing]
    models = ["billing.models"]
    dialects = ["postgres"]
    url_env = "BILLING_DATABASE_URL"
    ```

A model belongs to the database whose `models` list holds its module (or a parent package); a model no database claims is refused, and so is a foreign key from one database's model to another's. Each database's migrations live in `migrations/<name>/` unless `directory` says otherwise, and directories may not overlap. Commands name the database with `--database`, and code with `database="billing"`.

With one database, its name is `default` and its directory `migrations/`.

## Keys

### Per database

| Key | Type | Default | What it is |
| :--- | :--- | :--- | :--- |
| `models` | list of dotted modules | required | The modules that define this database's models. They are imported with the config file's directory, then `python_path`, first on `sys.path`. An import that registers no model is refused (no models is never read as "drop every table"). |
| `dialects` | list of `"postgres"`, `"sqlite"` | required | The target dialects: each DDL step is rendered once per dialect. |
| `url_env` | string | `"DATABASE_URL"` | The environment variable that holds the database URL. The URL itself is never in the file; `--url` overrides the variable. |
| `directory` | path | `migrations` (one database), `migrations/<name>` (several) | Where the migrations live, relative to the config file. |
| `tracking_schema` | string | none | Postgres only: the schema that holds the tracking tables, when not the connection's current schema. Refused unless `dialects` includes `"postgres"`. |
| `ddl_lock_timeout` | duration | `"5s"` | See below. |

### `ddl_lock_timeout`

How long a DDL statement Ferro runs on Postgres, in a migration step or in an auto-migrate pass, waits for its table lock before giving up: `"500ms"`, `"5s"`, `"1m"`, or `"0"` to wait without a limit (and without retries). A statement that gives up is retried with its step from the first statement, up to ten attempts, before the step fails naming `ddl_lock_timeout`. It does not apply to data steps, and it is not the wait for the run lock (`--lock-timeout`).

`connect()` with an auto-migrate flag names no database, so when several databases are configured they must all set the same `ddl_lock_timeout`; different values are refused, naming each one.

### Top level

| Key | Type | Default | What it is |
| :--- | :--- | :--- | :--- |
| `python_path` | list of paths | `[]` | Directories put on `sys.path` (after the config file's directory) before importing `models`, relative to the config file; for a `src/` layout, `python_path = ["src"]`. Shared by every database. |
| `databases` | table of tables | none | One table per database, as above. With it, no per-database key may sit at the top level. |

`unmanaged_tables` is reserved and refused until it ships ([#486](https://github.com/syn54x/ferro-orm/issues/486)).

## Lookup order

1. `FerroSettings(config=path)`, or `--config PATH` on the command line.
2. The `FERRO_CONFIG` environment variable.
3. The nearest directory, walking up from the working directory, that holds a `ferro.toml` or a `pyproject.toml` with a `[tool.ferro]` table.

The file found is used alone: nothing is layered on it, no environment variable overrides a key, and no `.env` file is read. These keys decide what a migration contains, so they come from the committed file only. No file is not an error for `FerroSettings()`: it returns an empty object listing the directories it searched, and refuses only when a database is asked for.

```python
from ferro import FerroSettings

settings = FerroSettings()
db = settings.database()          # the one configured; name it when there are several
db.directory                      # <config dir>/migrations
db.url_for()                      # $DATABASE_URL (or the url_env variable)
db.import_models()                # imports the models modules, returns this database's models
```

Each `FerroSettings()` reads the file again; nothing is cached. The file is read by pydantic-settings' `TomlConfigSettingsSource`, the only source `FerroSettings` keeps.

## Refusals

Every refusal names the file and the fix. The [CLI reference](cli.md#configuration-refusals) quotes three of them exactly; the rest:

| What | The refusal says |
| :--- | :--- |
| No config file found | where it searched, and to create a `ferro.toml`, add `[tool.ferro]`, or point `FERRO_CONFIG` / `--config` at one |
| `--config` / `FERRO_CONFIG` names a missing file | the resolved path, and to point it at a `ferro.toml` or a `pyproject.toml` with `[tool.ferro]` |
| A file that is not valid TOML | the parser's error, and to fix the file |
| `models` or `dialects` missing | the table, and the line to add (`models = ["myapp.models"]`, `dialects = ["postgres"]`) |
| An unknown key | the table, and the keys allowed there |
| `url` in the file | that the URL is never in the file: set `url_env`, or pass `--url` |
| A per-database key beside a `databases` table | to move it into `[databases.<name>]` |
| `python_path` inside a database table | to move it to the top level |
| `tracking_schema` without `"postgres"` in `dialects` | to add `"postgres"` or remove `tracking_schema` |
| An unparseable `ddl_lock_timeout` | the accepted forms: `"500ms"`, `"5s"`, `"1m"`, or `"0"` |
| Several databases and no `--database` | the configured names |
| Overlapping migration directories | both databases, and to set `directory` so neither is inside the other |
| A model no database claims | its module, and to add it (or a parent package) to a database's `models` |
| A foreign key across databases | both models and databases |
| `$DATABASE_URL` (or the `url_env` variable) unset | to export it or pass `--url` |

## See Also

- [CLI reference](cli.md) — the commands that read this file
- [Migrations](../guide/schema/migrations.md#install-and-set-up) — `ferro migrate init`
- [Multiple Databases](../howto/multiple-databases.md)
