# IR-first release checklist

Phase 7 release checklist for the public `v0.12.0` IR-first release with a
compatibility window through `v0.12.x`.

## Pre-release checklist

- [ ] All Phase 7 scoped issues are complete and synchronized:
  - [#100](https://github.com/syn54x/ferro-orm/issues/100)
  - [#101](https://github.com/syn54x/ferro-orm/issues/101)
  - [#102](https://github.com/syn54x/ferro-orm/issues/102)
  - [#103](https://github.com/syn54x/ferro-orm/issues/103)
- [ ] Deprecation warnings consistently include `Planned removal in v0.14.0`.
- [ ] Deprecated-compat inventory is still tracked via
      `pytest.mark.deprecated_operator_path`.
- [ ] User migration docs are complete:
  - `docs/plans/ir-first-migration-guide.md`
  - `docs/pages/howto/migrating-to-v0-12-0.md`
- [ ] Changelog release notes for `v0.12.0` are finalized.

## Verification matrix

- [ ] `cargo test --no-default-features --features testing`
- [ ] `cargo test -p ferro-schema-ir -p ferro-migrate`
- [ ] `uv run pytest -v tests/test_ir_vectors_contract.py`
- [ ] `uv run pytest -v --cov=src --cov-report=xml --cov-report=term`
- [ ] `uv run pytest -v -m "backend_matrix or postgres_only" --db-backends=sqlite,postgres`
- [ ] `uv run pytest -v -m deprecated_operator_path`
- [ ] `uv run pytest -v tests/test_query_builder.py tests/test_query_typing.py tests/test_session.py tests/test_alembic_bridge.py tests/test_docs_examples.py`
- [ ] `uv run zensical build --clean`
- [ ] `uv run maturin build --release`

## Exit-gate evidence

- [ ] Roadmap Phase 7 exit-gate checklist is updated with command evidence links.
- [ ] Migration guide is validated against at least one real example project.
- [ ] Issue comments contain verification summary and links to docs/evidence.

## PR closure requirements

If a PR is opened for Phase 7 completion, include explicit auto-close lines:

- `Closes #100`
- `Closes #101`
- `Closes #102`
- `Closes #103`
