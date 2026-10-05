# Config is `ferro.toml` or `pyproject.toml`, and everything reads it through `FerroSettings`

ADR-0036 put config in `[tool.ferro]` and rejected a second home. In-process migrations (ADR-0038) changed the weighing: a wheel, a Docker image or a frozen desktop app often ships without `pyproject.toml`, and those are the builds that call `ferro.migrations.up()` at start-up. So the same keys may live in a `ferro.toml`, without the `tool.ferro` prefix:

```toml
# ferro.toml
models    = ["myapp.models"]
dialects  = ["sqlite"]
directory = "migrations"

# several databases: [databases.<name>] in place of [tool.ferro.databases.<name>]
```

Every consumer (the CLI, `ferro.migrations.*`, the auto-migrate guard in `connect()`) reads config one way:

```python
from ferro import FerroSettings

settings = FerroSettings()                                   # the normal lookup
settings = FerroSettings(config=files("myapp") / "ferro.toml")   # a packaged app says where
```

The rules:

- **Lookup.** `FerroSettings(config=…)`, else `FERRO_CONFIG`, else the nearest directory, walking up from the working directory, that holds a `ferro.toml` or a `pyproject.toml` with a `[tool.ferro]` table. `--config` and `FERRO_CONFIG` accept either file.
- **Never both.** A directory holding a `ferro.toml` and a `pyproject.toml` with `[tool.ferro]` is refused, naming both. Nothing merges; ADR-0036's "selects and never layers" stands.
- **Paths are relative to the config file**, so a `ferro.toml` shipped beside its `migrations/` works wherever the package is installed.
- **The file is the only source.** `FerroSettings` is a pydantic-settings class whose sources are restricted to the config file: no environment variable overrides a field and no `.env` is loaded, for ADR-0036's reason. Unknown keys are refused. `pydantic-settings` becomes a core dependency.
- **No file is not an error for `FerroSettings()`.** It returns an empty settings object that says none was found and where it searched. A consumer that needs config refuses with that path; `connect()` carries on.
- **A malformed or incomplete file is an error for everyone**, including `connect(url, auto_migrate=True)`.
- **Defaults fill gaps in a file and never stand in for a missing one.** `directory` defaults to `migrations`, `url_env` to `DATABASE_URL`, `python_path` to empty, and `tracking_schema` to none, meaning the connection's current schema, which is only known once a connection exists. `models` and `dialects` have no default, and a file without them is refused at load.
- **Nothing is cached.** Each `FerroSettings()` reads the file again.
- **`ferro migrate init` asks which file to write.** The default is `pyproject.toml` when the project has one and `ferro.toml` otherwise; `--config-file` answers without a prompt.

## Considered options

- **`[tool.ferro]` only** (ADR-0036). Rejected now: the builds that most need runtime config are the ones that do not ship `pyproject.toml`.
- **Explicit arguments at runtime, no config lookup** (`up(directory=…, tracking_schema=…)`). Rejected: `tracking_schema` would be stated in two places that can disagree, and every consumer would grow its own argument list.
- **Environment overrides** (pydantic-settings' default). Rejected: `models`, `dialects`, `directory` and `tracking_schema` decide what a migration contains and where its record lives.
- **A plain pydantic model and `tomllib`.** Rejected: reading sources in the constructor is what `BaseSettings` is for, and the dependency is small.
- **A fixed default for `tracking_schema`.** Rejected: `"public"` would make every tenant schema on one server share a record, and a dedicated schema would refuse on every fresh database, since ferro never creates schemas.
- **A second URL for the tracking table.** Rejected: an atomic step commits its step record in the step's own transaction (ADR-0024); two connections are two transactions that can disagree. A privileged migrating role is the run's own URL or a named connection.
- **A cached settings singleton.** Rejected: a test that writes a config file would read a stale copy.

## Consequences

- ADR-0036 is amended: `ferro.toml` is a second home for the same keys, and "no `[tool.ferro]` found" becomes "no ferro config found".
- A local-first project puts `ferro.toml` and its migrations directory inside the package.
- `connect()` with an auto-migrate flag now fails on a malformed config file it previously never read.
