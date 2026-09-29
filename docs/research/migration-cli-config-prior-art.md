---
title: How Python migration CLIs find models and the database URL
type: research
tags: [migrations, cli, config, alembic, django, aerich, piccolo, prisma, cyclopts, click, typer, argparse]
related_files:
  - src/ferro/__init__.py
  - src/ferro/session.py
  - src/ferro/metaclass.py
  - src/ferro/migrations/alembic.py
  - pyproject.toml
related_issues: [461, 452, 472]
captured: 2026-09-28
---

# How Python migration CLIs find models and the database URL

Research for [#461](https://github.com/syn54x/ferro-orm/issues/461), on the
[#452](https://github.com/syn54x/ferro-orm/issues/452) wayfinder map. It feeds
the decision ticket [#472](https://github.com/syn54x/ferro-orm/issues/472)
("Config and model discovery for the CLI") and makes no decision itself.

**The question.** Ferro has no CLI today. A `ferro migrate …` command has to do
two things before it can plan anything: import the application's models (so
Ferro's registry is populated, exactly as `connect()` needs it) and find a
database URL. Alembic solves both with a user-owned `env.py`. What do the other
tools do, what do their users complain about, and what CLI framework fits a
library that must not bloat its install?

## Summary

1. **Nobody discovers models by walking the filesystem.** Every tool surveyed
   makes the user *name* the modules that hold models, and imports them for
   the side effect of registration: Alembic through a hand-written import in
   `env.py`, Django through `INSTALLED_APPS` (each app's `models` submodule is
   imported by `django.setup()`), Tortoise/aerich through a list of dotted
   module paths in the `TORTOISE_ORM` dict, Piccolo through `APP_REGISTRY` of
   dotted `piccolo_app` modules whose `AppConfig` lists table classes (or
   `table_finder(modules=[...])`, which still takes module names). Prisma is
   the outlier because its models are not Python: it reads a schema file.
2. **The database URL is treated as a deployment secret and kept out of the
   committed config.** Alembic's own pyproject template says so in as many
   words; Django needs `dj-database-url` to read `DATABASE_URL`; Prisma went
   from `url = env("DATABASE_URL")` in the schema to an explicit
   `process.env['DATABASE_URL']` in `prisma.config.ts` with `import
   'dotenv/config'`. The convention that has won across ecosystems is a
   `DATABASE_URL`-style environment variable with a CLI flag to override it.
3. **Config lives in `pyproject.toml` `[tool.<name>]` when it is source-level
   (where the models are, where migrations go) and outside it when it is
   deployment-level (URL, logging).** Alembic 1.16 drew exactly that line.
   aerich writes `[tool.aerich]` with `tortoise_orm`, `location`,
   `src_folder`. Every tool also accepts `-c/--config <file>` and most accept
   an environment variable (`ALEMBIC_CONFIG`, `DJANGO_SETTINGS_MODULE`,
   `PICCOLO_CONF`) that points at the config; precedence is always
   flag > env var > default file in the working directory.
4. **Multiple databases are handled by naming: one alias per database,
   one migration lineage per alias, and the command takes the alias.**
   Django's `migrate --database=<alias>` migrates one database at a time;
   Alembic's `multidb` template loops over `databases = a, b` with a
   `[a]`/`[b]` section each; aerich's `--app` selects the Tortoise app (and
   through it the connection); Piccolo scopes every migration command by app
   name. Ferro's `connect(name=…)`, `engines.session(name)` and
   `Model.using(name)` already give it that alias.
5. **CLI framework.** `argparse` is what Alembic and Django use and costs
   nothing. `click` (Pallets) has no runtime dependencies. `typer` now
   vendors click and always installs `rich`, `shellingham` and
   `annotated-doc` (the slim variant was discontinued in 0.22.0). `cyclopts`
   (single maintainer, very fast release cadence, 5.0.0 on 2026-09-23) installs
   `attrs`, `docstring-parser`, `rich` and `rich-rst`, and is the only one
   with first-class `[tool.<name>]` + env-var + CLI merging built in. For a
   library, the ecosystem convention is a `[project.optional-dependencies]
   cli = [...]` extra plus a console script that fails with an
   "install `ferro-orm[cli]`" message when the extra is absent (httpx does
   this verbatim), because `[project.scripts]` installs the entry point
   regardless of extras.

## Ferro today

Facts the findings below are measured against, from the tree at `8a0e89d`:

- **No CLI, no config file.** `pyproject.toml` declares `dependencies =
  ["pydantic>=2.0"]`, one extra (`alembic = ["alembic>=1.18.1",
  "sqlalchemy>=2.0.46"]`) and no `[project.scripts]`. `requires-python =
  ">=3.13"`.
- **Models register at class definition.** The metaclass calls
  `REGISTRY.register(cls)` in `src/ferro/metaclass.py`; importing the module
  is the registration. Nothing in `src/ferro/` reads `os.environ`; there is
  no URL convention today.
- **Connections are named.** `connect(url, ..., name=None, default=False,
  ...)` in `src/ferro/__init__.py` registers the unnamed connection as
  `"default"`; `engines.session(name)` (`src/ferro/session.py`) opens a
  session on a named connection; `Model.using("name")`
  (`src/ferro/models.py`) binds one model's operations to it. The connections
  guide shows two connections in one process
  (`connect(..., name="app", default=True)`; `connect(..., name="analytics")`).
- **The Alembic bridge is env.py-shaped.** `docs/pages/guide/migrations.md`
  tells the user to write `from myapp.models import Comment, Post, User  #
  noqa: F401 — importing registers models` and `target_metadata =
  get_metadata()` in `migrations/env.py`, and to pass
  `render_item=render_item` to `context.configure`. The URL stays wherever
  Alembic's `env.py` puts it.
- **The scaffold this project's docs assume** (`scaffold-python-project`
  skill) generates a `cyclopts>=4` CLI at `cli/app.py` wired through
  `[project.scripts]`, a `pydantic-settings` `Settings` with a `db_dsn`
  property read from `<MODULE>_`-prefixed env vars and `.env`, and a
  `storage/db.py` that does `import <module>.storage.models  # noqa: F401:
  register metadata before connect` and then `connect(db.db_dsn,
  migrate_updates=True)`. That is the side-effect-import pattern every tool
  below also relies on.

## Alembic

**Config file.** "Alembic looks in the current directory for this file when
any other commands are run; to indicate an alternative location, the
`--config` option may be used, or the `ALEMBIC_CONFIG` environment variable
may be set" ([tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)).
In `alembic/config.py` the flag is `-c/--config` with `action="append"` and
help text "Alternate config file; defaults to value of ALEMBIC_CONFIG
environment variable, or "alembic.ini""; the code special-cases an
`ALEMBIC_CONFIG` whose basename is `pyproject.toml`, and `Config.__init__`
takes both `file_` and `toml_file`
([config.py](https://github.com/sqlalchemy/alembic/blob/main/alembic/config.py)).

**`pyproject.toml`.** Since 1.16.0, `alembic init --template pyproject
alembic` writes a `[tool.alembic]` section (`script_location =
"%(here)s/alembic"`, `prepend_sys_path = ["."]`, file templates, path
handling). The docs draw the line explicitly: "Use of `pyproject.toml` does
not preclude having an `alembic.ini` file as well, as `alembic.ini` is still
the default location for **deployment** details such as database URLs,
connectivity options, and logging to be present" — and, "as connectivity and
logging is consumed only by user-managed code within the `env.py` file, it is
feasible to have an environment that does not require the `alembic.ini` file
itself to be present at all"
([tutorial, pyproject section](https://alembic.sqlalchemy.org/en/latest/tutorial.html#using-pyproject-toml)).
`--name/-n` (default `"alembic"`, "Name of section in .ini file to use") picks
a section and applies to ini files only, not to pyproject
([config.py](https://github.com/sqlalchemy/alembic/blob/main/alembic/config.py);
[cookbook: multiple environments](https://alembic.sqlalchemy.org/en/latest/cookbook.html#run-multiple-alembic-environments-from-one-ini-file)).
The feature was requested in 2020 ([#708](https://github.com/sqlalchemy/alembic/issues/708),
[#720](https://github.com/sqlalchemy/alembic/issues/720)) and re-opened as
"separate source code and deployment configurations"
([#1082](https://github.com/sqlalchemy/alembic/issues/1082)) before shipping
in 1.16; early bugs in the TOML path followed (booleans not accepted, fixed in
1.16.4; `truncate_slug_length` rejected,
[#1709](https://github.com/sqlalchemy/alembic/issues/1709)).

**`env.py`.** The user-owned script "configures and generates a SQLAlchemy
engine, procures a connection … and invokes the migration engine"; it reads
`sqlalchemy.url` from the config (`config.get_main_option("sqlalchemy.url")`
or `engine_from_config(prefix="sqlalchemy.")`) and defines
`run_migrations_online()` / `run_migrations_offline()`
([tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)).
Templates: `generic`, `pyproject` (1.16.0), `async`, `multidb`
(`alembic list_templates`, same page).

**Model discovery.** None. Autogenerate needs "a `MetaData` object … loaded
in `env.py` and then passed to `EnvironmentContext.configure()`", shown as
`from myapp.mymodel import Base; target_metadata = Base.metadata`
([autogenerate](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)).
The footgun is documented: when the database has tables absent from the
metadata, "the autogenerate process will normally assume these are
extraneous tables in the database to be dropped, and it will generate a
`drop_table()` directive for each"
([autogenerate: omitting table names](https://alembic.sqlalchemy.org/en/latest/autogenerate.html#omitting-table-names-from-the-autogenerate-process)).
The maintainer's standard answer to "autogenerate wants to drop everything"
threads is to check that env.py imports every module that defines tables
([#709](https://github.com/sqlalchemy/alembic/issues/709),
[discussion #1416](https://github.com/sqlalchemy/alembic/discussions/1416),
[discussion #1046](https://github.com/sqlalchemy/alembic/discussions/1046)
for SQLModel). `target_metadata` may be a list of `MetaData` objects
"consulted in order", with unique table keys required across them
([autogenerate: multiple metadata](https://alembic.sqlalchemy.org/en/latest/autogenerate.html#autogenerating-multiple-metadata-collections)).

**`-x` arguments.** `-x` is `action="append"`, "Additional arguments consumed
by custom env.py scripts" ([config.py](https://github.com/sqlalchemy/alembic/blob/main/alembic/config.py)).
`EnvironmentContext.get_x_argument(as_dictionary=True)` parses `key=value`
tokens; the documented example is passing the URL itself: `alembic -x
dbname=postgresql://user:pass@host/dbname upgrade head` read as
`context.get_x_argument(as_dictionary=True).get('dbname')`
([runtime API](https://alembic.sqlalchemy.org/en/latest/api/runtime.html#alembic.runtime.environment.EnvironmentContext.get_x_argument)).
The cookbook uses it for conditional data migrations (`-x data=true`)
([cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html#conditional-migration-elements)).
So Alembic's answer to "override the URL" is: the user writes the override in
`env.py`, from whatever source they like.

**URL from the environment in practice.** The FastAPI full-stack template's
`env.py` does `from app.models import SQLModel  # noqa`,
`target_metadata = SQLModel.metadata`, `def get_url(): return
str(settings.DATABASE_URL)`, and injects it with
`configuration["sqlalchemy.url"] = get_url()` (online) or
`context.configure(url=url, ...)` (offline)
([env.py](https://github.com/fastapi/full-stack-fastapi-template/blob/master/backend/app/alembic/env.py)).
SQLModel's own docs defer migrations to the Advanced User Guide and mention
Alembic only in passing (`alter_column`'s `postgresql_using`)
([SQLModel: datetime](https://sqlmodel.tiangolo.com/advanced/datetime/),
[tutorial](https://sqlmodel.tiangolo.com/tutorial/create-db-and-table/)).

**Multiple databases.** The `multidb` template's ini declares `databases =
engine1, engine2` and "for multiple database configuration, new named
sections are added which each include a distinct `sqlalchemy.url` entry"
(`[engine1]` / `[engine2]`)
([alembic.ini.mako](https://github.com/sqlalchemy/alembic/blob/main/alembic/templates/multidb/alembic.ini.mako)).
Its `env.py` reads `db_names = config.get_main_option("databases", "")`,
builds one engine per name with
`engine_from_config(context.config.get_section(name, {}), prefix="sqlalchemy.")`,
keeps `target_metadata = {}` keyed by name, and configures each with
`upgrade_token="%s_upgrades" % name` so one revision file carries a
per-database section
([env.py](https://github.com/sqlalchemy/alembic/blob/main/alembic/templates/multidb/env.py)).
The alternative is one `--name` section per lineage, each with its own
`script_location` and `alembic_version` table
([cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html#run-multiple-alembic-environments-from-one-ini-file)).

**Footprint.** Alembic 1.20.0, `requires-python >=3.10`, depends on
`SQLAlchemy>=2.0`, `Mako`, `typing-extensions>=4.12`, `tomli` (<3.11)
([PyPI](https://pypi.org/pypi/alembic/json)). The CLI is `argparse`
(`from argparse import ArgumentParser`; `class CommandLine`; commands are
registered by introspecting `alembic.command`)
([config.py](https://github.com/sqlalchemy/alembic/blob/main/alembic/config.py)).

## Django

**Settings module.** `DJANGO_SETTINGS_MODULE` is "in Python path syntax, e.g.
`mysite.settings`", and the module must be on `sys.path`
([settings](https://docs.djangoproject.com/en/stable/topics/settings/#designating-the-settings)).
`manage.py` "does the same thing as `django-admin` but also sets the
`DJANGO_SETTINGS_MODULE` environment variable"; every command takes
`--settings SETTINGS` ("If this isn't provided, `django-admin` will use the
`DJANGO_SETTINGS_MODULE` environment variable") and `--pythonpath
PYTHONPATH` ("Adds the given filesystem path to the Python `sys.path`")
([django-admin](https://docs.djangoproject.com/en/stable/ref/django-admin/#cmdoption-settings)).
The alternative is `settings.configure(**kwargs)` in code; exactly one of the
two is required, and using neither raises when a setting is first accessed
([settings](https://docs.djangoproject.com/en/stable/topics/settings/#either-configure-or-django-settings-module-is-required)).

**Model discovery.** `django.setup()` populates the app registry in three
stages over `INSTALLED_APPS`: import each app config, then "Django attempts
to import the `models` submodule of each application. You must define or
import all models in your application's `models.py` or
`models/__init__.py`", then run `ready()`
([applications](https://docs.djangoproject.com/en/stable/ref/applications/#initialization-process)).
`setup()` is called for you by management commands and the WSGI/ASGI entry
points and "must be called explicitly in standalone Python scripts"; the
troubleshooting section lists `AppRegistryNotReady` from forgetting it
([applications](https://docs.djangoproject.com/en/stable/ref/applications/#troubleshooting)).
So Django's discovery is *declared* (a list of packages) and *imported* (the
`models` submodule by convention), not scanned.

**Database URL.** Django has no URL. `DATABASES` is a dict of aliases to
connection dicts (`ENGINE`, `NAME`, `USER`, ...), and "the `default` alias is
special" — it must exist, though it may be empty if routers cover every model
([multi-db](https://docs.djangoproject.com/en/stable/topics/db/multi-db/#defining-your-databases)).
Reading a 12-factor `DATABASE_URL` is a third-party convention:
`dj-database-url` "allows you to utilize the 12factor inspired `DATABASE_URL`
environment variable to configure your Django application", used as
`DATABASES["default"] = dj_database_url.config(default=..., conn_max_age=600)`
([README](https://github.com/jazzband/dj-database-url/blob/master/README.rst)).

**Multiple databases.** "`migrate` operates on one database at a time",
defaulting to `default`, so multi-database projects run `./manage.py migrate
--database=users` per alias; `DATABASE_ROUTERS` classes implement
`allow_migrate(db, app_label, model_name=None, **hints)` to decide whether a
model's migrations run on a given alias, and cross-database relations are
unsupported
([multi-db](https://docs.djangoproject.com/en/stable/topics/db/multi-db/#synchronizing-your-databases)).
`migrate` also takes `--fake`, `--plan`, `--check`, `--run-syncdb`,
`--noinput`; `makemigrations [app_label ...]` limits generation to named apps
and has `--check` and `--merge`
([django-admin](https://docs.djangoproject.com/en/stable/ref/django-admin/#django-admin-migrate)).

**Framework.** Management commands subclass `BaseCommand` and receive an
`argparse` parser in `add_arguments(parser)`
([django-admin](https://docs.djangoproject.com/en/stable/ref/django-admin/)).

## aerich (Tortoise ORM)

**Config.** `aerich init -t settings.TORTOISE_ORM` takes "Tortoise-ORM config
module dict variable, like settings.TORTOISE_ORM" (required), `--location`
(default `./migrations`) and `-s/--src_folder` ("Folder of the source,
relative to project root"), and writes them to `pyproject.toml` under
`[tool.aerich]` as `tortoise_orm`, `location`, `src_folder`. The top-level
CLI takes `-c, --config TEXT  Config file. [default: pyproject.toml]` and
`--app TEXT  Tortoise-ORM app name`
([README](https://github.com/tortoise/aerich/blob/dev/README.md)).
The config is therefore a *dotted path to a Python object*, resolved by
import; the `src_folder` exists because that import fails when the project
root is not on `sys.path`, which is the recurring complaint:
"Error while importing configuration module: No module named 'app'"
([#188](https://github.com/tortoise/aerich/issues/188)), "Problems with
app.models" after setting all three keys
([#233](https://github.com/tortoise/aerich/issues/233)), "init error No such
file 'pyproject.toml'" when run outside the project root
([#217](https://github.com/tortoise/aerich/issues/217)), and a `KeyError:
'src_folder'` on upgrade in the changelog
([CHANGELOG](https://github.com/tortoise/aerich/blob/dev/CHANGELOG.md)).

**Model discovery and URL.** Both live in the one dict Tortoise already
needs at runtime: `"connections": {"default": "mysql://..."}` and
`"apps": {"models": {"models": ["tests.models", "aerich.models"],
"default_connection": "default"}}`; the migration tool's own model
(`aerich.models`) must be listed, in exactly one app
([README](https://github.com/tortoise/aerich/blob/dev/README.md)).
Tortoise's `init` accepts the same dict as `config=`, a `db_url=` +
`modules={"app": ["app.models"]}` shorthand, or a `config_file` (JSON/YAML);
the dict form "is useful when you want to configure different databases for
different applications"
([Tortoise setup](https://tortoise.github.io/setup.html)).
Reusing the runtime's config object means aerich never has its own notion
of "where are the models" or "what is the URL": the app answers once.

**Multiple databases.** An app maps to a connection through
`default_connection`; `aerich --app models_second migrate` selects the app,
and `--location './{app}/migrations'` keeps one migrations directory per app
([README](https://github.com/tortoise/aerich/blob/dev/README.md)).

**Footprint.** `requires-python >=3.10`; runtime deps `tortoise-orm`,
`dictdiffer`, `asyncclick>=8.3`, `anyio`; TOML writing is an extra
(`aerich[toml]` → `tomli-w`, `tomlkit`); `[project.scripts] aerich =
"aerich.cli:main"` ([pyproject.toml](https://github.com/tortoise/aerich/blob/dev/pyproject.toml)).

## Piccolo

**Config.** `piccolo_conf.py` holds `DB` ("Contains your database settings",
an engine instance) and `APP_REGISTRY` ("Is used for registering Piccolo
Apps"). "The `piccolo_conf.py` file should be at the root of your project" so
the CLI can import it from the working directory; `PICCOLO_CONF` overrides
the module, e.g. `export PICCOLO_CONF=conf.piccolo_conf_local`, and the docs
recommend it for tests and per-environment deployments
([projects](https://piccolo-orm.readthedocs.io/en/latest/piccolo/projects_and_apps/piccolo_projects.html#location);
[engines](https://piccolo-orm.readthedocs.io/en/latest/piccolo/engines/index.html#piccolo-conf-environment-variable)).
`piccolo project new` scaffolds it.

**Model discovery.** `APP_REGISTRY = AppRegistry(apps=["blog.piccolo_app"])`
lists dotted module paths; each `piccolo_app.py` has `APP_CONFIG =
AppConfig(app_name="blog", migrations_folder_path=..., table_classes=[Author,
Post])`, or `table_classes=table_finder(modules=["blog.tables"])` which
imports the named modules and collects `Table` subclasses, with
`include_tags`/`exclude_tags` filters
([apps](https://piccolo-orm.readthedocs.io/en/latest/piccolo/projects_and_apps/piccolo_apps.html#table-finder)).
Still module names, never a directory walk.

**URL.** `DB = PostgresEngine(config={"host": ..., "database": ..., "user":
..., "password": ...})`; "The config dictionary is passed directly to the
underlying database adapter, asyncpg"
([PostgresEngine](https://piccolo-orm.readthedocs.io/en/latest/piccolo/engines/postgres_engine.html#config)).
Because `piccolo_conf.py` is Python, the env-var pattern is whatever the user
writes there; the docs' only lever is `PICCOLO_CONF` swapping the whole file.

**Multiple databases.** Migrations are scoped by app: `piccolo migrations new
<app> --auto`, `forwards <app>` or `forwards all`, `backwards <app> <id>`,
`check`, `--fake`, `--preview`; files land in each app's `piccolo_migrations/`
([create](https://piccolo-orm.readthedocs.io/en/latest/piccolo/migrations/create.html);
[running](https://piccolo-orm.readthedocs.io/en/latest/piccolo/migrations/running.html#forwards)).
Tables can bind an engine explicitly (`class MyTable(Table, db=DB)`)
([engines](https://piccolo-orm.readthedocs.io/en/latest/piccolo/engines/index.html#explicit)),
but the migration docs describe one `DB` per project.

**Footprint.** Piccolo 1.36.0 depends on `black`, `colorama`, `Jinja2`,
`targ>=0.7.0`, `inflection`, `typing-extensions`, `pydantic[email]==2.*`
([PyPI](https://pypi.org/pypi/piccolo/json)); its CLI framework `targ` is the
same author's "Build a Python CLI for your app, just using type hints and
docstrings", depending on `colorama` and `docstring-parser`
([PyPI](https://pypi.org/pypi/targ/json)).

## Prisma

Prisma is not Python, but the ticket names it because its config story moved
twice in two major versions and each move is instructive.

**v6 and earlier.** One `schema.prisma` with `datasource db { provider =
"postgresql"; url = env("DATABASE_URL") }`; "You can only have one
`datasource` block in a schema"
([schema reference](https://www.prisma.io/docs/orm/reference/prisma-schema-reference#datasource)).
The CLI auto-loads `.env` from, in order, `./.env`, the `--schema` folder,
the `package.json`-declared schema folder, `./prisma`, and "if a `.env` file
is located in step 1., but additional, clashing `.env` variables are located
in steps 2. - 4., the CLI will throw an error"
([environment variables](https://www.prisma.io/docs/orm/v7/more/dev-environment/environment-variables#using-an-env-file)).

**v7.** `prisma.config.ts` became the stable CLI config: discovered as
`prisma.config.*` / `.config/prisma.*`, overridable with `--config`; it holds
`schema` (default `./prisma/schema.prisma` then `./schema.prisma`),
`datasource.url` (required), `datasource.shadowDatabaseUrl`, and
`migrations.path`; `directUrl` was removed. "Environment variables from
`.env` files need to be loaded explicitly. The `prisma init` command
generates a config that includes `import 'dotenv/config'` by default", and
its `env()` helper "throws an error if the specified environment variable is
not defined"
([config reference](https://www.prisma.io/docs/orm/v7/reference/prisma-config-reference)).
Schema lookup order is `--schema` flag, then the config file, then the
defaults ([schema location](https://www.prisma.io/docs/orm/v7/prisma-schema/overview/location)).
`migrate dev` / `deploy` / `diff` / `status` / `resolve` all take `--schema`
and `--config`
([CLI reference](https://www.prisma.io/docs/orm/v7/reference/prisma-cli-reference)).

**v8.** The `datasource` and `generator` blocks "are gone: the connection
URL and the file paths are set in `prisma.config.ts` instead", the schema is
now `contract.prisma`, and the config reads
`db: { connection: process.env['DATABASE_URL']! }`
([schema location, v8](https://www.prisma.io/docs/orm/prisma-schema/overview/location)).
The trajectory: magic `env()` in a declarative file plus implicit `.env`
discovery → an explicit code-level config file that loads the environment on
purpose and names the variable in plain code.

**Multiple databases.** One datasource per schema; `schemas = [...]` plus
`@@schema` handles multiple *Postgres/CockroachDB/SQL Server schemas* inside
one database, "not available for SQLite and MySQL"
([multi-schema](https://www.prisma.io/docs/orm/prisma-schema/data-model/multi-schema)).
Prisma Client Python (`pip install prisma`) wraps the same CLI and
`DATABASE_URL` convention
([setup](https://prisma-client-py.readthedocs.io/en/stable/getting_started/setup/)).

## Cross-tool comparison

| | Models found by | URL found by | Committed config | Point CLI at config | Multiple databases |
|---|---|---|---|---|---|
| Alembic | user imports in `env.py` → `target_metadata` | `sqlalchemy.url` in ini, or anything `env.py` computes (`-x`, env var) | `alembic.ini` (deploy) + `[tool.alembic]` (source, 1.16+) | `-c`, `ALEMBIC_CONFIG`, cwd | `multidb` template (`databases = a, b`, per-section URL) or `--name` per lineage |
| Django | `INSTALLED_APPS` → import `<app>.models` in `django.setup()` | `DATABASES` dict in settings; `DATABASE_URL` via `dj-database-url` | `settings.py` (a module) | `DJANGO_SETTINGS_MODULE`, `--settings`, `--pythonpath` | alias per db; `migrate --database=<alias>`; routers `allow_migrate` |
| aerich | `apps.<app>.models` list in `TORTOISE_ORM` dict | `connections.<name>` in the same dict | `[tool.aerich]` in `pyproject.toml` (dotted path to the dict) | `-c` (default `pyproject.toml`) | app ↔ connection; `--app`; `{app}` in `--location` |
| Piccolo | `APP_REGISTRY` → `piccolo_app.APP_CONFIG.table_classes` / `table_finder(modules)` | `DB = Engine(config=...)` in `piccolo_conf.py` | `piccolo_conf.py` (a module) | `PICCOLO_CONF`, cwd | app-scoped commands; one `DB` |
| Prisma v7/v8 | schema/contract file | `prisma.config.ts` + `process.env` (explicit dotenv) | `prisma.config.ts` | `--config`, `--schema` | one datasource per schema |

Three patterns recur:

- **The config is code, not data, wherever the URL is involved.** `env.py`,
  `settings.py`, `piccolo_conf.py`, `prisma.config.ts`, and aerich's dotted
  path to a Python dict. Only Alembic's ini and aerich's `[tool.aerich]` are
  pure data, and both delegate the URL back to code (env.py / the dict).
- **A dotted path is the unit of "where".** `settings.TORTOISE_ORM`,
  `mysite.settings`, `blog.piccolo_app`, `myapp.models`. Every one of them
  needs the project root importable, and the tools that run from arbitrary
  directories grow a knob for it (`--pythonpath`, `src_folder`,
  `prepend_sys_path`). aerich's issue tracker is the cautionary tale for
  leaving that implicit.
- **Overrides compose flag > env var > file > default, and the flag is the
  file's *location*, not its contents.** `--config`/`-c` names a file;
  `--settings`/`-t`/`PICCOLO_CONF` name a module. None of them merge two
  config files. Only cyclopts (below) merges CLI, env and TOML *values*.

## Where config lives: `[tool.<name>]` vs dedicated file vs env

- `[tool.<name>]` in `pyproject.toml` is the reserved, tool-specific table in
  the packaging standard, and both Alembic (1.16) and aerich write there.
  Alembic's split — source-level settings in pyproject, "deployment details
  such as database URLs" elsewhere — is the clearest statement of the
  convention ([tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html#using-pyproject-toml)).
- A dedicated code file (`env.py`, `piccolo_conf.py`, `prisma.config.ts`)
  buys arbitrary logic (compute the URL, pick a tenant) at the cost of
  boilerplate the tool must scaffold and the user must keep in sync.
  Alembic's `-x` exists precisely because `env.py` is the only place a
  runtime knob can be honoured.
- Environment variables are the deployment channel everywhere: `DATABASE_URL`
  (12-factor, via `dj-database-url`, Prisma, the FastAPI template's
  `settings.DATABASE_URL`), and a *locator* variable for the config itself
  (`ALEMBIC_CONFIG`, `DJANGO_SETTINGS_MODULE`, `PICCOLO_CONF`).
- `--config` in every surveyed tool selects *which* file, with the env
  locator as the second choice and the working directory's default file as
  the third. No surveyed tool layers a `--config` file on top of a default
  one.

## CLI framework options

Numbers are from PyPI JSON and the GitHub repository pages on 2026-09-28.

| | Version / Python | Runtime dependencies | Maintainers | Cadence (last 12 months) | Config merging | Notes |
|---|---|---|---|---|---|---|
| `argparse` | stdlib | none | CPython | n/a | none; `fromfile_prefix_chars` only | "the default recommended standard library module for implementing basic command line applications"; `add_subparsers(dest=, required=)` ([docs](https://docs.python.org/3/library/argparse.html)). Used by Alembic and Django. |
| `click` 8.5.0 | `>=3.10` | none declared in `pyproject.toml` (`requires_dist` empty on PyPI) | Pallets org; 17.8k stars, 75 open issues | 7 releases 2025-09 → 2026-06 | none | "Click is a Python package for creating beautiful command line interfaces in a composable way"; lazy-loading subcommands documented ([repo](https://github.com/pallets/click), [PyPI](https://pypi.org/pypi/click/json), [pyproject](https://github.com/pallets/click/blob/main/pyproject.toml)). aerich uses the async fork `asyncclick`; httpx's `[cli]` extra uses click. |
| `typer` 0.27.2 | `>=3.10` | `shellingham`, `rich`, `annotated-doc`, `colorama` (Windows); click vendored since 0.26.0 | fastapi org; 20k stars | 9 releases 2025-09 → 2026-02 | none | "There used to be a slimmed-down version of Typer called `typer-slim` … since version 0.22.0, we have stopped supporting this, and `typer-slim` now simply installs (all of) Typer"; `TYPER_USE_RICH=0` disables rich output but not its install ([docs](https://typer.tiangolo.com/#optional-dependencies), [PyPI](https://pypi.org/pypi/typer/json), [repo](https://github.com/fastapi/typer)). |
| `cyclopts` 5.0.0 | `>=3.11` | `attrs`, `docstring-parser`, `rich`, `rich-rst` | single maintainer (Brian Pugh); 1.3k stars, 12 open issues | 4.0.0 → 4.25.3 → 5.0.0; ten releases in September 2026 alone; 5.0.0 on 2026-09-23 dropped 3.10 and changed parsing | first-class: `App(config=[cyclopts.config.Env("FERRO_"), cyclopts.config.Toml("pyproject.toml", root_keys=["tool", "ferro"], search_parents=True)])`; "CLI arguments override everything else; environment variables override TOML values; TOML file provides the base configuration; Python default" | "Intuitive, easy CLIs based on python type hints"; own parser (not click); Pydantic/dataclass/attrs parameter types ([config docs](https://cyclopts.readthedocs.io/en/latest/config_file.html), [PyPI](https://pypi.org/pypi/cyclopts/json), [repo](https://github.com/BrianPugh/cyclopts), [releases](https://github.com/BrianPugh/cyclopts/releases)). The scaffold skill generates `cyclopts>=4`. |

Two ecosystem mechanics that apply whatever framework is picked:

- **Extras and console scripts.** `[project.optional-dependencies]` lets a
  user `pip install your-project-name[gui]`; `[project.scripts]` makes a
  command that runs "the equivalent of `import sys; from spam import
  main_cli; sys.exit(main_cli())`"
  ([packaging guide](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#creating-executable-scripts)).
  The script is installed whether or not the extra is; httpx therefore
  declares `cli = ["click==8.*", "pygments==2.*", "rich>=10,<15"]` and
  `httpx = "httpx:main"`, and `httpx/__init__.py` wraps `from ._main import
  main` in `try/except ImportError` with a fallback `main()` that prints
  "The httpx command line client could not run because the required
  dependencies were not installed. Make sure you've installed everything
  with: pip install 'httpx[cli]'" and exits 1
  ([pyproject](https://github.com/encode/httpx/blob/master/pyproject.toml),
  [`__init__.py`](https://github.com/encode/httpx/blob/master/httpx/__init__.py),
  [docs](https://www.python-httpx.org/)). With `argparse` no extra is needed
  at all; with click the extra is one package; with typer or cyclopts it is
  four.
- **Ad-hoc invocation.** `uvx --from 'mypy[faster-cache]' mypy` shows the
  extras syntax for running a package's console script without installing
  it into the project, and `--from` is also how a command whose name differs
  from its package is invoked ([uv tools](https://docs.astral.sh/uv/guides/tools/)).
  A `ferro` console script would be reachable as `uvx --from 'ferro-orm[cli]'
  ferro migrate …` for free.

## Multiple databases against Ferro's shape

Ferro's runtime already has what the surveyed tools converge on: a named
alias per database (`connect(url, name="analytics")`, `"default"` when
unnamed), an alias-selecting session (`engines.session("analytics")`) and a
per-model binding (`Model.using("analytics")`). What no tool surveyed does is
infer the alias from the models; all of them make the *command* take it:

- Django: `migrate --database=<alias>`, one run per alias, routers decide
  which models belong ([multi-db](https://docs.djangoproject.com/en/stable/topics/db/multi-db/#synchronizing-your-databases)).
- Alembic: a `databases = a, b` list with one URL section each and one
  `engine`/`target_metadata` pair per name in `env.py`
  ([multidb env.py](https://github.com/sqlalchemy/alembic/blob/main/alembic/templates/multidb/env.py)).
- aerich: `--app <name>` where the app's `default_connection` names the
  database, and `{app}` in the migrations location
  ([README](https://github.com/tortoise/aerich/blob/dev/README.md)).
- Piccolo: every migration command is `<app_name>` or `all`
  ([running](https://piccolo-orm.readthedocs.io/en/latest/piccolo/migrations/running.html#forwards)).

The shared shape is: **one migrations directory per alias, one URL per
alias, the alias is a positional or `--flag` on the command, and the model →
alias mapping lives in the config, not in the model.** Ferro's registry is
global (one `REGISTRY`, one compiled modelset) and its `using=` is a query-time
binding, so the open question for #472 is where the *schema-level* "these
models belong to `analytics`" fact would live — the tools surveyed put it in
the config (Django routers, aerich app → connection, Alembic's per-name
`target_metadata`), never on the class.

## What this evidence points at for #472

Findings only; the decision is #472's.

- **Model location as a list of dotted modules is the universal answer**,
  and Ferro's registration-by-import already matches it (the scaffold's
  `import <module>.storage.models  # noqa: F401` line). A directory walk
  would be novel and none of the surveyed tools do it. Whatever holds that
  list must also make the project root importable, or the CLI inherits
  aerich's `src_folder` bug class; Alembic's `prepend_sys_path = ["."]` in
  `[tool.alembic]` and Django's `--pythonpath` are the precedents.
- **URL out of the committed file, in by `DATABASE_URL`-style env var, with a
  `--url` flag override,** is where every ecosystem landed, including Prisma
  after two redesigns. Alembic's "pyproject for source, ini for deployment"
  split is the exact vocabulary.
- **`[tool.ferro]` in `pyproject.toml`** is the conventional home for the
  source-level part (module list, migrations directory, aliases) and is what
  both Python tools with a data config chose; a dedicated `ferro.toml` has no
  precedent in the survey, and a `ferro_conf.py` would reproduce the
  `env.py` the ticket wants to avoid.
- **Overrides compose flag > env > file.** If the framework is cyclopts that
  composition is built in and reads `[tool.ferro]` natively; with
  click/argparse it is a small amount of Ferro code.
- **Framework.** The install-footprint ranking is argparse (0) < click (0
  runtime deps, one package in a `[cli]` extra) < cyclopts (4) ≈ typer (4,
  slim variant gone). Cyclopts is what the scaffold this project assumes
  and the only one with config merging; it is also a one-person project on
  a major-version bump this month. Either way the `httpx` pattern (extra +
  graceful console-script fallback) is the shape to copy.

## Sources

- Alembic: [tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html), [autogenerate](https://alembic.sqlalchemy.org/en/latest/autogenerate.html), [cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html), [runtime API](https://alembic.sqlalchemy.org/en/latest/api/runtime.html), [config.py](https://github.com/sqlalchemy/alembic/blob/main/alembic/config.py), [multidb alembic.ini.mako](https://github.com/sqlalchemy/alembic/blob/main/alembic/templates/multidb/alembic.ini.mako), [multidb env.py](https://github.com/sqlalchemy/alembic/blob/main/alembic/templates/multidb/env.py), [PyPI](https://pypi.org/pypi/alembic/json), issues [#708](https://github.com/sqlalchemy/alembic/issues/708), [#709](https://github.com/sqlalchemy/alembic/issues/709), [#720](https://github.com/sqlalchemy/alembic/issues/720), [#1082](https://github.com/sqlalchemy/alembic/issues/1082), [#1709](https://github.com/sqlalchemy/alembic/issues/1709), discussions [#1046](https://github.com/sqlalchemy/alembic/discussions/1046), [#1416](https://github.com/sqlalchemy/alembic/discussions/1416).
- SQLModel / FastAPI: [SQLModel datetime](https://sqlmodel.tiangolo.com/advanced/datetime/), [full-stack template env.py](https://github.com/fastapi/full-stack-fastapi-template/blob/master/backend/app/alembic/env.py).
- Django: [settings](https://docs.djangoproject.com/en/stable/topics/settings/), [django-admin](https://docs.djangoproject.com/en/stable/ref/django-admin/), [applications](https://docs.djangoproject.com/en/stable/ref/applications/), [multi-db](https://docs.djangoproject.com/en/stable/topics/db/multi-db/), [dj-database-url](https://github.com/jazzband/dj-database-url/blob/master/README.rst).
- aerich / Tortoise: [README](https://github.com/tortoise/aerich/blob/dev/README.md), [pyproject.toml](https://github.com/tortoise/aerich/blob/dev/pyproject.toml), [CHANGELOG](https://github.com/tortoise/aerich/blob/dev/CHANGELOG.md), issues [#188](https://github.com/tortoise/aerich/issues/188), [#217](https://github.com/tortoise/aerich/issues/217), [#233](https://github.com/tortoise/aerich/issues/233), [Tortoise setup](https://tortoise.github.io/setup.html).
- Piccolo: [projects](https://piccolo-orm.readthedocs.io/en/latest/piccolo/projects_and_apps/piccolo_projects.html), [apps](https://piccolo-orm.readthedocs.io/en/latest/piccolo/projects_and_apps/piccolo_apps.html), [engines](https://piccolo-orm.readthedocs.io/en/latest/piccolo/engines/index.html), [PostgresEngine](https://piccolo-orm.readthedocs.io/en/latest/piccolo/engines/postgres_engine.html), [creating migrations](https://piccolo-orm.readthedocs.io/en/latest/piccolo/migrations/create.html), [running migrations](https://piccolo-orm.readthedocs.io/en/latest/piccolo/migrations/running.html), [PyPI piccolo](https://pypi.org/pypi/piccolo/json), [PyPI targ](https://pypi.org/pypi/targ/json).
- Prisma: [schema reference](https://www.prisma.io/docs/orm/reference/prisma-schema-reference), [environment variables (v7)](https://www.prisma.io/docs/orm/v7/more/dev-environment/environment-variables), [config reference (v7)](https://www.prisma.io/docs/orm/v7/reference/prisma-config-reference), [schema location (v7)](https://www.prisma.io/docs/orm/v7/prisma-schema/overview/location), [CLI reference (v7)](https://www.prisma.io/docs/orm/v7/reference/prisma-cli-reference), [schema location (v8)](https://www.prisma.io/docs/orm/prisma-schema/overview/location), [multi-schema](https://www.prisma.io/docs/orm/prisma-schema/data-model/multi-schema), [Prisma Client Python setup](https://prisma-client-py.readthedocs.io/en/stable/getting_started/setup/).
- CLI frameworks: [argparse](https://docs.python.org/3/library/argparse.html), [click PyPI](https://pypi.org/pypi/click/json), [click pyproject](https://github.com/pallets/click/blob/main/pyproject.toml), [click repo](https://github.com/pallets/click), [typer optional dependencies](https://typer.tiangolo.com/#optional-dependencies), [typer PyPI](https://pypi.org/pypi/typer/json), [typer repo](https://github.com/fastapi/typer), [cyclopts config files](https://cyclopts.readthedocs.io/en/latest/config_file.html), [cyclopts PyPI](https://pypi.org/pypi/cyclopts/json), [cyclopts repo](https://github.com/BrianPugh/cyclopts), [cyclopts releases](https://github.com/BrianPugh/cyclopts/releases).
- Packaging: [writing pyproject.toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/), [httpx pyproject](https://github.com/encode/httpx/blob/master/pyproject.toml), [httpx `__init__.py`](https://github.com/encode/httpx/blob/master/httpx/__init__.py), [httpx docs](https://www.python-httpx.org/), [uv tools](https://docs.astral.sh/uv/guides/tools/).
