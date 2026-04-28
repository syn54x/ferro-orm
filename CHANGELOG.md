# CHANGELOG


## Unreleased

### Features

- Add `Transaction.execute / fetch_all / fetch_one` and top-level `ferro.execute / fetch_all / fetch_one` for raw SQL inside or outside a transaction. `transaction()` now yields a `Transaction` handle. ([#31](https://github.com/syn54x/ferro-orm/issues/31))
- Add `ForeignKey(index=True)` to emit a non-unique index on the shadow `*_id` column. Combining with `unique=True` is redundant and raises a `UserWarning`. ([#32](https://github.com/syn54x/ferro-orm/issues/32))

### Behavior Changes

- Alembic autogenerate now emits single-column index names as `idx_<table>_<col>` (was `ix_<table>_<col>`) so that schemas generated through Alembic match the Rust runtime DDL emitter byte-for-byte. This eliminates phantom drop+create diffs when running `alembic revision --autogenerate` against a database bootstrapped by `connect(auto_migrate=True)`. **Existing `FerroField(index=True)` users will see a one-time rename diff on their next autogen run** — review it once, accept the rename, and subsequent autogens will be clean. The new cross-emitter DDL parity invariant is documented in `AGENTS.md`. ([#32](https://github.com/syn54x/ferro-orm/issues/32))


## v0.4.0 (2026-04-27)

### Bug Fixes

- Correct BackRef type hinting for all/first
  ([`6171923`](https://github.com/syn54x/ferro-orm/commit/617192328d77a8159c671ed6e469dc489c462e42))

### Features

- Redesign relationship declarations
  ([`911e77d`](https://github.com/syn54x/ferro-orm/commit/911e77d15a63df893543bfe6500c5283e8f066f3))


## v0.3.4 (2026-04-25)

### Bug Fixes

- Serialize UUID M2M query contexts
  ([`f53b3ca`](https://github.com/syn54x/ferro-orm/commit/f53b3ca4219d3cd21174d1cb2215bda717c0ac3d))

### Chores

- Gitignore .worktrees/ for local worktrees
  ([`142cd3f`](https://github.com/syn54x/ferro-orm/commit/142cd3fc1240e2e0ce5597b170455e4355ac98b9))

- Update lock file
  ([`fa1c003`](https://github.com/syn54x/ferro-orm/commit/fa1c003efd3960c4c7a647ddf0f8ba166c731e01))

### Documentation

- Add backend guide
  ([`78f1e29`](https://github.com/syn54x/ferro-orm/commit/78f1e295052663416e37ce2bef81be06ec602ba0))

### Refactoring

- Replace Any backend with typed engine
  ([`71628a7`](https://github.com/syn54x/ferro-orm/commit/71628a7281e7f6d8ec6a4640eb2512a7589a634d))

### Testing

- Add local Postgres test provider
  ([`f8601a5`](https://github.com/syn54x/ferro-orm/commit/f8601a54b414baefd5f1078470c60b3ee85782db))

- Harden bridge-boundary coverage
  ([`f1a6064`](https://github.com/syn54x/ferro-orm/commit/f1a60647a799a17ad8adf75c86e9635dd192cc55))


## v0.3.3 (2026-04-24)

### Bug Fixes

- Cast NULL and strings to ::uuid for Postgres using catalog
  ([`f5cb4f0`](https://github.com/syn54x/ferro-orm/commit/f5cb4f08ceaf0763a29c3b78d4d077ca1119fc1c))

- Catalog casts for date/timestamp columns on Postgres
  ([`95ef5ca`](https://github.com/syn54x/ferro-orm/commit/95ef5cadc28eb26481c38b51dbca1b370a883d10))

- Clean up rebase conflicts with main
  ([`716511c`](https://github.com/syn54x/ferro-orm/commit/716511c829021ee6d2390bb85c877e670c1d7631))

- Enum OIDs
  ([`a9867be`](https://github.com/syn54x/ferro-orm/commit/a9867beac242a9d630aeb7e49b718a4234c541ec))

- Postgres native enums on save and StrEnum schema registration
  ([`44277e1`](https://github.com/syn54x/ferro-orm/commit/44277e1922182b020c17d9a7a2a9e99dd62061e5))

- Use Postgres SQL dialect when connecting to postgres URLs
  ([`c627ac8`](https://github.com/syn54x/ferro-orm/commit/c627ac8e4fa84555e0cc7250f73ce6f0858125a3))

- **postgres**: Add dual-db ORM test matrix
  ([`1fa657f`](https://github.com/syn54x/ferro-orm/commit/1fa657fe4335d41214fcb24b1eac5dcf3138273f))

- **postgres**: Bind boolean writes as booleans
  ([`346441a`](https://github.com/syn54x/ferro-orm/commit/346441a073a540c857a8aaa67bf4029cb4099535))

- **postgres**: Cast uuid columns to text in SELECT for Any decode
  ([`df957c0`](https://github.com/syn54x/ferro-orm/commit/df957c0202d32608843d6a24ae4c924ed5b9381d))

- **postgres**: Cast UUID filter params for sqlx Any compatibility
  ([`889cf8b`](https://github.com/syn54x/ferro-orm/commit/889cf8b61131c2d53e8414a76ca7b2dbc7868c23))

- **postgres**: Decode native enum columns via text cast
  ([`1270f9d`](https://github.com/syn54x/ferro-orm/commit/1270f9dcd1cc5aa19cf484c3d9c3bb3a82255a05))

### Refactoring

- Expand db matrix coverage and harden postgres paths
  ([`b82f3ac`](https://github.com/syn54x/ferro-orm/commit/b82f3ac886459861cdfde122b99b880b85c09a61))

- Multi db architecture with true sqlite and postgres support
  ([`459a0c5`](https://github.com/syn54x/ferro-orm/commit/459a0c5f9c8a95ecacc9ba552137252d34de4824))

### Testing

- Expand schema constraints into db matrix
  ([`24a7f0a`](https://github.com/syn54x/ferro-orm/commit/24a7f0ad38b90e98a41cf32fe2777d988ff7047f))


## v0.3.2 (2026-04-24)

### Bug Fixes

- Move alembic reqs to optional dependencies
  ([`87f0e81`](https://github.com/syn54x/ferro-orm/commit/87f0e8157640ac9984da20e0c4c7290dbfcf4bfd))

### Build System

- **sqlx**: Enable rustls TLS for PostgreSQL connections
  ([`807fa81`](https://github.com/syn54x/ferro-orm/commit/807fa8196a3742a5a50380fe6dbf727045798cc3))

### Chores

- Sync uv.lock with project version 0.3.1
  ([`c3c9f91`](https://github.com/syn54x/ferro-orm/commit/c3c9f91907a3ece8ce7bc70f08979f1dd269a87c))

### Continuous Integration

- Build preflight wheels earlier to fail faster
  ([`475c93c`](https://github.com/syn54x/ferro-orm/commit/475c93caa1d51fb07d7eda875205086761d64e8f))

- Fix linux-aarch64 wheel builds for ring/rustls asm
  ([`5eadddc`](https://github.com/syn54x/ferro-orm/commit/5eadddc922b51448ad4da3841a84fd4931b19814))

- Gate release on preflight wheel builds for all platforms
  ([`6ec48a2`](https://github.com/syn54x/ferro-orm/commit/6ec48a275b8dc9868612a3f374595ce63fe151ca))

- Restore legacy release workflow
  ([`d3ee87c`](https://github.com/syn54x/ferro-orm/commit/d3ee87c68995a1163480eb9be8a3111032e94842))

### Documentation

- Add Supabase PostgreSQL connection and TLS guidance
  ([`b1d61ad`](https://github.com/syn54x/ferro-orm/commit/b1d61ad4395a17c7c2270c1d4776500c436c22d1))


## v0.3.1 (2026-04-23)

### Bug Fixes

- Alembic autogenerate named SQLAlchemy enums for PostgreSQL
  ([`25a00e8`](https://github.com/syn54x/ferro-orm/commit/25a00e84502ae1f8ba502718934d93eedfa4ce09))

- **migrations**: Align nullable inference with field types
  ([`885f0fe`](https://github.com/syn54x/ferro-orm/commit/885f0fe155dfa643e29b9425ff1ede62f3f0b269))

- **migrations**: Propagate ForeignKey(unique=True) to Alembic metadata
  ([#22](https://github.com/syn54x/ferro-orm/pull/22),
  [`9329e8f`](https://github.com/syn54x/ferro-orm/commit/9329e8fba2f0efd201bea4545393654c7d1dd34e))

### Continuous Integration

- Fix release
  ([`e2822f6`](https://github.com/syn54x/ferro-orm/commit/e2822f6c9bacc6fc955e56b2ca8e120cc22b0b72))

- Fix release
  ([`e5c1adc`](https://github.com/syn54x/ferro-orm/commit/e5c1adcc10eb44845ef95d78226840ecdbfd0ebd))

- Fix release
  ([`688d01b`](https://github.com/syn54x/ferro-orm/commit/688d01bdae0aff1f82e4a1bb60dd1b8ab35e1d01))

### Documentation

- Prefer Field over FerroField
  ([`3385cfa`](https://github.com/syn54x/ferro-orm/commit/3385cfadf0951f80827dac1aa08f73430a02023f))


## v0.3.0 (2026-04-23)

### Bug Fixes

- Align composite unique index names and harden Alembic/Rust handling
  ([`3350481`](https://github.com/syn54x/ferro-orm/commit/33504812d37d93bf69c2be8f6bee6f390803a460))

- Refresh Pydantic FieldInfo when reconciling shadow FK types
  ([`6cf1ac8`](https://github.com/syn54x/ferro-orm/commit/6cf1ac8c2e361df8de11795a8151c34d17a39445))

### Chores

- Remove doc
  ([`16e4028`](https://github.com/syn54x/ferro-orm/commit/16e4028f72fc47109b2511d1feb23811c831f32c))

### Continuous Integration

- Fix release
  ([`249e460`](https://github.com/syn54x/ferro-orm/commit/249e46058bac87215920083a9d45557f3c58b62f))

- Fix release
  ([`888e15e`](https://github.com/syn54x/ferro-orm/commit/888e15eff693d1e1bfa279d809e790e52cd7ce25))

- Fix release
  ([`58bb5b2`](https://github.com/syn54x/ferro-orm/commit/58bb5b2b0962481b3cfbf3ffbe6c6a2653b213c0))

- Reorder release steps to prevent tagging before checks are complete
  ([`ad1fd8d`](https://github.com/syn54x/ferro-orm/commit/ad1fd8d5ba08bdd7a1bcd257fff3fc12ff458c12))

### Documentation

- Complete documentation restructure and implementation summary
  ([`937e75e`](https://github.com/syn54x/ferro-orm/commit/937e75ee7b5c526aca776dd8409f9e0df5f0e892))

- Enhance shadow field documentation and clarify relationship resolution process
  ([`1d350fd`](https://github.com/syn54x/ferro-orm/commit/1d350fd728310a5b9a24f129986f873a84a8592f))

### Features

- Composite unique constraints and default M2M pair uniqueness
  ([`dc12880`](https://github.com/syn54x/ferro-orm/commit/dc12880b7b8676c088183edf1f32b48a36314448))

- Derive shadow FK types from related PK and reconcile after resolve
  ([`d3ae486`](https://github.com/syn54x/ferro-orm/commit/d3ae4862858ccd51f62d62a939e6a90b8efb8980))

### Testing

- UUID FK save reparenting and bulk_create coverage
  ([`6c93cea`](https://github.com/syn54x/ferro-orm/commit/6c93cea7906ac264b266342ecf71602c7aff6ed6))


## v0.2.1 (2026-04-20)

### Bug Fixes

- Defer annotations resolution
  ([`edd39ab`](https://github.com/syn54x/ferro-orm/commit/edd39abdec7b34410040394d430fd30833e02aee))

### Chores

- Update patch_tags in pyproject.toml to include refactor
  ([`36c29a7`](https://github.com/syn54x/ferro-orm/commit/36c29a71f0f95845d50d1dd6fdbc14b2c4b20ac2))

### Continuous Integration

- Fix release & mkdocs publish workflows
  ([`630dc7c`](https://github.com/syn54x/ferro-orm/commit/630dc7cea32da02602acc037e1d8da722d3fb593))

### Documentation

- Restructure documentation following Diátaxis framework
  ([`b3c2cde`](https://github.com/syn54x/ferro-orm/commit/b3c2cde1d0bde589ad0f08a34002202bca81e5e5))

- Update BackRef references and enhance field documentation
  ([`baf73ba`](https://github.com/syn54x/ferro-orm/commit/baf73ba03abd554ca8159bc718aa1785b08691ae))

- Update model field annotations to support optional back references
  ([`2044896`](https://github.com/syn54x/ferro-orm/commit/20448966854d33e64c03a97831f640af279e93b4))

### Refactoring

- Enhance model relationship descriptors and improve field handling
  ([`6275ebb`](https://github.com/syn54x/ferro-orm/commit/6275ebb9be3ce7a419fb84c381c0a90eec22a5e9))

- Modularize metaclass __new__ method for easier testing and maintenance
  ([`e514b95`](https://github.com/syn54x/ferro-orm/commit/e514b950cb94e333418f7bd556b8e5b48bf7298e))

- Rename BackRelationship to BackRef and add back_ref to Field
  ([`d24d32d`](https://github.com/syn54x/ferro-orm/commit/d24d32d3402b51cb738ac2a2c8f396b98d4de632))

- Update demo_queries to use BackRef instead of BackRelationship
  ([`51799ad`](https://github.com/syn54x/ferro-orm/commit/51799adc1a0b90f937cab7651cf8276f53a16100))

### Testing

- Update references from BackRelationship to BackRef in test files
  ([`60a1d87`](https://github.com/syn54x/ferro-orm/commit/60a1d87d88fe93996dc0b479c75d40aad3ff143b))


## v0.2.0 (2026-02-14)

### Chores

- **.gitignore**: Remove src/ferro/fields.py from ignore list
  ([`1c46851`](https://github.com/syn54x/ferro-orm/commit/1c46851abe49b55fb7582759b4a2a1d812803199))

- **changelog**: Fix changelog format
  ([`579bb10`](https://github.com/syn54x/ferro-orm/commit/579bb109579c4b4f93712a523f36d1f783702c20))

### Continuous Integration

- **docs**: Publish docs site and relax strict commit checks
  ([`9b2af96`](https://github.com/syn54x/ferro-orm/commit/9b2af96684e8208ee0d17ce1e57df15063153ea0))

- **release**: Consolidate changelog and release workflow orchestration
  ([`2724bcc`](https://github.com/syn54x/ferro-orm/commit/2724bcc67f40343f341dffdd923735ccf127ef52))

- **release**: Update permissions for publish workflow
  ([`02ecf9f`](https://github.com/syn54x/ferro-orm/commit/02ecf9f07a48916467b33daa9d55b5b7312d777c))

- **release**: Update permissions for publish workflow
  ([`d9d7243`](https://github.com/syn54x/ferro-orm/commit/d9d724399aa6005716b3d4b9fc0b2bde76cffe8a))

- **release**: Update workflows for PyPI Trusted Publishing
  ([`75195d5`](https://github.com/syn54x/ferro-orm/commit/75195d5d612448dedc00bada3fc2be6097bb82cb))

### Features

- **fields**: Add wrapped Field helper for ferro metadata
  ([`2795ed9`](https://github.com/syn54x/ferro-orm/commit/2795ed9b86f93bd8b35591a40dd3e29b133b3026))


## v0.1.1 (2026-02-13)

### Chores

- **project**: Refine tooling configuration and code quality gates
  ([`d91aadd`](https://github.com/syn54x/ferro-orm/commit/d91aaddb0ac6764833d0742ae18bf0a897e5fe4a))

- **query**: Update demo script and dependency metadata
  ([`b737b12`](https://github.com/syn54x/ferro-orm/commit/b737b129fd257fe69eba9417dab6674c554afcfb))

- **release**: Publish v0.1.0-rc.1
  ([`a37d0d4`](https://github.com/syn54x/ferro-orm/commit/a37d0d44e2397cf23f05b3153685ddbfc435ab91))

- **release**: Publish v0.1.0-rc.2
  ([`529801a`](https://github.com/syn54x/ferro-orm/commit/529801ac051f2b16d05ef58626fb646479eb3247))

- **release**: Publish v0.1.1
  ([`c9ee751`](https://github.com/syn54x/ferro-orm/commit/c9ee751c198ea50e1aed5b38781a1d2f3cf53b65))

### Continuous Integration

- Optimize caching and split PR vs main test execution
  ([`e84344c`](https://github.com/syn54x/ferro-orm/commit/e84344cfe88c67b747d46ae289bda97ecb8f7772))

- **docs**: Add MkDocs build and deploy workflows
  ([`363ffa1`](https://github.com/syn54x/ferro-orm/commit/363ffa18255d1861c97eb807ab7437a052dc12db))

- **release**: Add end-to-end CI, publish, and changelog pipelines
  ([`1589dda`](https://github.com/syn54x/ferro-orm/commit/1589dda5502c71c5553cc870d3a4d4364fd49e48))

- **release**: Configure changelog generation and release token wiring
  ([`9b95e41`](https://github.com/syn54x/ferro-orm/commit/9b95e415b89b4862a6fdbc62985ec4df0ec63d2c))

- **release**: Enable prerelease publication path
  ([`eab9a18`](https://github.com/syn54x/ferro-orm/commit/eab9a1827f8882cfdb35c7e065dd9dcf90d402c4))

- **release**: Stabilize workflow stages and macOS/toolchain settings
  ([`dbf9a3e`](https://github.com/syn54x/ferro-orm/commit/dbf9a3ec49a964d4b1136c0f64f97d98e65bf0ae))

### Documentation

- **api**: Reorganize docs structure and validate code examples
  ([`cd5b7b2`](https://github.com/syn54x/ferro-orm/commit/cd5b7b29554076b6f9f06be4b087144e1ea3c4fe))

- **community**: Add contributor and release documentation set
  ([`f9fb40e`](https://github.com/syn54x/ferro-orm/commit/f9fb40e5e9ab638d2743626da0f30730fa698eb1))

- **readme**: Clean duplicated content and streamline guidance
  ([`785573a`](https://github.com/syn54x/ferro-orm/commit/785573a9e06f355408b30b95a063e9f94da01dc1))

- **site**: Add MkDocs structure and ORM usage guides
  ([`d5b4955`](https://github.com/syn54x/ferro-orm/commit/d5b4955943d2ece669f2afc5cb7d61f42af14d9d))

### Features

- **connection**: Add pool management and schema registration APIs
  ([`2fa9fd7`](https://github.com/syn54x/ferro-orm/commit/2fa9fd794b0586d77175ee076a2777f30dfa224b))

- **core**: Add async CRUD engine and identity map bridge
  ([`64ea39f`](https://github.com/syn54x/ferro-orm/commit/64ea39f10ccb06a77ee6985ebfe8aecfe702ca0b))

- **logging**: Route Ferro logs through Python logging
  ([`df6be66`](https://github.com/syn54x/ferro-orm/commit/df6be66111fe7494513a5bbd284ece95fbbc2172))

- **migrations**: Integrate Alembic-backed migration management
  ([`c244996`](https://github.com/syn54x/ferro-orm/commit/c244996838bc3cfd98421ad71b7a18aff5d391ba))

- **query**: Add fluent query builder and predicate execution
  ([`11d0a5c`](https://github.com/syn54x/ferro-orm/commit/11d0a5c6b409e59c21ba03b50989555024d8c1cd))

- **relations**: Add relationship descriptors and query node modules
  ([`c8e72bd`](https://github.com/syn54x/ferro-orm/commit/c8e72bd0c5769574469711332d78855bb04151d2))

### Testing

- **core**: Add integration coverage for CRUD and schema behavior
  ([`e5b3e51`](https://github.com/syn54x/ferro-orm/commit/e5b3e51bbed1e98bb486dd8aa6b4ccf112d891dd))

- **query**: Add coverage for builder operations and advanced types
  ([`6e33f40`](https://github.com/syn54x/ferro-orm/commit/6e33f400990ce82aa4732e0122e95b95a2431a57))

- **relations**: Cover one-to-one behavior and schema constraints
  ([`7cc8377`](https://github.com/syn54x/ferro-orm/commit/7cc83779fff8839c2519703eb41083f1f907656f))


## v0.1.0 (2026-02-13)

- Initial Release
