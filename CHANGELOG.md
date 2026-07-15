# CHANGELOG


## v0.16.2 (2026-07-15)

### Bug Fixes

- Self-referential FKs no longer evict their component from CREATE TABLE order
  ([#303](https://github.com/syn54x/ferro-orm/pull/303),
  [`d1a55f6`](https://github.com/syn54x/ferro-orm/commit/d1a55f6ae241101d98ee659e6651fa33abaf4ec5))

### Refactoring

- Compile QueryIR payloads at a single wire choke point
  ([`851b3f4`](https://github.com/syn54x/ferro-orm/commit/851b3f4e1f077d70481ebca643ff0ffff81645d8))


## v0.16.1 (2026-07-13)

### Bug Fixes

- Chunk bulk_create under backend bind-parameter limits
  ([`38da62d`](https://github.com/syn54x/ferro-orm/commit/38da62d9ab9fab6b36a9eae0722c1d741b976d15))


## v0.16.0 (2026-07-13)

### Chores

- ADR-0009 aggregate projections + output-alias/traversed-projection/aggregate-projection glossary
  ([`51b8c0c`](https://github.com/syn54x/ferro-orm/commit/51b8c0c5fef2d383561b0127028ad5e7eb5bbbc3))

### Documentation

- Aggregation guide, projection reference, typing page
  ([#296](https://github.com/syn54x/ferro-orm/pull/296),
  [`44230c5`](https://github.com/syn54x/ferro-orm/commit/44230c55b53544788210f0664dc2a32634409839))

### Features

- Global aggregates — count/sum/avg/min/max ([#294](https://github.com/syn54x/ferro-orm/pull/294),
  [`1679e44`](https://github.com/syn54x/ferro-orm/commit/1679e44399b8ecc9f47a4f416dbf9aa821173b16))

- Grouped aggregates, order_by rules, verb guardrails
  ([#295](https://github.com/syn54x/ferro-orm/pull/295),
  [`2408ad7`](https://github.com/syn54x/ferro-orm/commit/2408ad7ae91cf6e7024e7297764191967596e869))

- Traversed projection + output aliases ([#293](https://github.com/syn54x/ferro-orm/pull/293),
  [`1e73e2a`](https://github.com/syn54x/ferro-orm/commit/1e73e2a8f53a4d56d3caaa38a754166023e4c361))

### Refactoring

- QueryIR v5 — the expr record-field shape ([#292](https://github.com/syn54x/ferro-orm/pull/292),
  [`dc779d0`](https://github.com/syn54x/ferro-orm/commit/dc779d0e1184e6c87e19872302a045c3f36b8e5f))


## v0.15.0 (2026-07-11)

### Chores

- ADR-0008 populated relations + include/populated-relation glossary
  ([`a1b273d`](https://github.com/syn54x/ferro-orm/commit/a1b273dc71b2b0f77e08e69808281ab5c25ffaea))

- Amend registration adr with review outcomes (operation-seam sync, build-then-swap, deregistration)
  ([`fcb8db3`](https://github.com/syn54x/ferro-orm/commit/fcb8db385ef5c835121420b0d90c4a9bbfd8a1f8))

- New adrs and context
  ([`3ea617c`](https://github.com/syn54x/ferro-orm/commit/3ea617c2ff418aa91fdf2d72470826b0aeec7242))

- Pin failed-resolve retryability in registration adr
  ([`d5895ba`](https://github.com/syn54x/ferro-orm/commit/d5895bae311cf9a2cff230b16a975aa1a1e9a2ad))

- Pin zero-DDL, single-flight, and pure-Python clean-path invariants in registration adr
  ([`09673f3`](https://github.com/syn54x/ferro-orm/commit/09673f334128cba7af7d94db38c6f43acd241d37))

- Registration adr
  ([`05c5732`](https://github.com/syn54x/ferro-orm/commit/05c5732125370d1e2b539f83f89e200190c67bd0))

- Relation traversal ADR and context
  ([`9151856`](https://github.com/syn54x/ferro-orm/commit/9151856364ea6ccc8ca125102302b5115e6a9ae0))

- Triage and update old plan statuses
  ([`2f3e3c0`](https://github.com/syn54x/ferro-orm/commit/2f3e3c0584eec37815e2520852077b9c08885546))

### Continuous Integration

- **release**: Custom highlights atop the GitHub Release notes
  ([#275](https://github.com/syn54x/ferro-orm/pull/275),
  [`f705938`](https://github.com/syn54x/ferro-orm/commit/f70593839af93ec902a0537f0f1ae73a34c425c2))

### Documentation

- ADR-0007 materialization plan + complete-instance glossary
  ([`4acc879`](https://github.com/syn54x/ferro-orm/commit/4acc879b23fcd5bc540b5ed7ee7129d95124ec77))

### Features

- Atomic bulk registration install with fingerprint gate (#244)
  ([#251](https://github.com/syn54x/ferro-orm/pull/251),
  [`20c6061`](https://github.com/syn54x/ferro-orm/commit/20c6061c376faab78a7e3dc2496e5dd3cc123115))

- Compile ModelCodecPlan from SchemaIR at registration
  ([#239](https://github.com/syn54x/ferro-orm/pull/239),
  [`7e15fdc`](https://github.com/syn54x/ferro-orm/commit/7e15fdcb9f32938857ad21838feca978591fd044))

- Generation-counter dirty tracking with assemble-not-recompile (#245)
  ([#252](https://github.com/syn54x/ferro-orm/pull/252),
  [`2790e56`](https://github.com/syn54x/ferro-orm/commit/2790e566437d6e67f1f5c3abe555162ebb62598d))

- Joined-row hydration — QueryIR v4 + populated relations via include()
  ([#289](https://github.com/syn54x/ferro-orm/pull/289),
  [`4335417`](https://github.com/syn54x/ferro-orm/commit/4335417cbfd33da95b6ea52971d6f5e805af9ad7))

- JSONB column support (#260) ([#266](https://github.com/syn54x/ferro-orm/pull/266),
  [`19f8cee`](https://github.com/syn54x/ferro-orm/commit/19f8cee3b7be3a570265f1269c735ac9093ab16a))

- Partial materialization — QueryIR v3 + partial selects (Rows/Row)
  ([#283](https://github.com/syn54x/ferro-orm/pull/283),
  [`e44129b`](https://github.com/syn54x/ferro-orm/commit/e44129bf966fa349b5bf637a5976c99b88c644b2))

- Query-time joins — relation traversal for filter and sort (stage 1)
  ([#276](https://github.com/syn54x/ferro-orm/pull/276),
  [`55dc386`](https://github.com/syn54x/ferro-orm/commit/55dc386f087fd6506d1713c9333bc11103bc96ac))

- Sync registration at ORM operation seam (#247)
  ([#254](https://github.com/syn54x/ferro-orm/pull/254),
  [`8409322`](https://github.com/syn54x/ferro-orm/commit/84093226a86b2cdc17afdce00d36fc4782c614c4))

### Refactoring

- Canonicalize registry keys — derive register_model key from model identity
  ([#250](https://github.com/syn54x/ferro-orm/pull/250),
  [`d835c96`](https://github.com/syn54x/ferro-orm/commit/d835c96f08094ca0a8f7df0947ba9440f956edc9))

- Centralize register/deregister registry entrypoints (#243)
  ([#248](https://github.com/syn54x/ferro-orm/pull/248),
  [`2f2be69`](https://github.com/syn54x/ferro-orm/commit/2f2be69ac9dc859621afa5938f08b7f167af1870))

- Compile ColumnSpec column facts once (#255) ([#256](https://github.com/syn54x/ferro-orm/pull/256),
  [`75fe1de`](https://github.com/syn54x/ferro-orm/commit/75fe1de5f3aee8f870e1e189f639b7466445c6dd))

- Drop RegisteredModel.schema — IR-first registry cleanup
  ([#241](https://github.com/syn54x/ferro-orm/pull/241),
  [`fc3b9f9`](https://github.com/syn54x/ferro-orm/commit/fc3b9f9c50bbd80f3915663e6278712df35fee96))

- Remove legacy migration shims and shadow runtime
  ([#237](https://github.com/syn54x/ferro-orm/pull/237),
  [`c2117b3`](https://github.com/syn54x/ferro-orm/commit/c2117b3351189912436c90552e127018db438c34))

### Testing

- Pin provisional import — Python-only registration until connect (#246)
  ([#253](https://github.com/syn54x/ferro-orm/pull/253),
  [`ca111b5`](https://github.com/syn54x/ferro-orm/commit/ca111b5672c647cbb021fd63bf59038f0028f303))


## v0.14.0 (2026-07-07)

### Bug Fixes

- **ff-g**: Make Postgres db_check ADD CONSTRAINT idempotent (G6, #176)
  ([#181](https://github.com/syn54x/ferro-orm/pull/181),
  [`2c0e4cf`](https://github.com/syn54x/ferro-orm/commit/2c0e4cf6c922a73454198f6aa0aa75b30eb1f0c8))

### Chores

- **benchmarks**: Pinned async benchmark suite over the rich-type hot path
  ([#196](https://github.com/syn54x/ferro-orm/pull/196),
  [`d9a656b`](https://github.com/syn54x/ferro-orm/commit/d9a656b320bd7fb6b613f4408a796161e9badc41))

### Documentation

- Consolidate migration guides into one evergreen upgrade guide
  ([#234](https://github.com/syn54x/ferro-orm/pull/234),
  [`15c83db`](https://github.com/syn54x/ferro-orm/commit/15c83dbc6b1c057a78dfc425d70441e50c359f5d))

- Fixes roadmap
  ([`5718d02`](https://github.com/syn54x/ferro-orm/commit/5718d02630b6c84d3ced6f8a3080adf4e7fe2383))

- **fable-fixes**: Fold #176 into Epic FF-G as sub-task G6
  ([`b966e65`](https://github.com/syn54x/ferro-orm/commit/b966e65739d7c33ed666a7fd14086a27688516dd))

- **ff-a**: A5 — docs & migration guide for the mutation-surface changes
  ([#180](https://github.com/syn54x/ferro-orm/pull/180),
  [`ffc5476`](https://github.com/syn54x/ferro-orm/commit/ffc5476f9ba72f03a35048f283b11390f813856b))

- **ff-b**: Tick FF-B sub-task and exit-gate boxes in the fable-fixes roadmap
  ([`29f9172`](https://github.com/syn54x/ferro-orm/commit/29f91724e2b6886f8299bbb42940c804c8e88141))

### Features

- **ff-a**: Create() is a real INSERT; save() distinguishes INSERT from UPDATE (A3+A4)
  ([#179](https://github.com/syn54x/ferro-orm/pull/179),
  [`de3ec30`](https://github.com/syn54x/ferro-orm/commit/de3ec30196202ee14c42afc5b02f163f82a68451))

- **ff-a**: Reject limit/offset on mutating queries
  ([#178](https://github.com/syn54x/ferro-orm/pull/178),
  [`67faf42`](https://github.com/syn54x/ferro-orm/commit/67faf421d49984c5be16d2163982f13ab86cc5e4))

- **ff-a**: Typed DBAPI-shaped exception hierarchy mapped from sqlx errors
  ([#177](https://github.com/syn54x/ferro-orm/pull/177),
  [`9c306c5`](https://github.com/syn54x/ferro-orm/commit/9c306c549e716dd4a5158d655265aa7890e63c0b))

- **ff-b**: B1 canonical derived-type & naming decision table + refusal-rail scaffolding
  ([`8c879f7`](https://github.com/syn54x/ferro-orm/commit/8c879f77833a7e5a983d53012efc8332cca5b852))

- **ff-b**: B2+B6 one derived-type decision table; native PG enums + timestamptz/time parity; delete
  bridge mirrors
  ([`bfdc1fd`](https://github.com/syn54x/ferro-orm/commit/bfdc1fda1eceebe70ae711e38b785af88b46e11e))

- **ff-b**: B3+B4 single-source artifact naming; both emitters emit named fk_/uq_ artifacts
  ([`6b771f3`](https://github.com/syn54x/ferro-orm/commit/6b771f38933c72e8f848013a5ae93708fabf5c7a))

- **ff-c**: C1 — per-model ColumnCodec plan; delete codec.rs schema sniffing (F5)
  ([#197](https://github.com/syn54x/ferro-orm/pull/197),
  [`0e8e572`](https://github.com/syn54x/ferro-orm/commit/0e8e57252f4cca2d2bc9fffd06d9155338056cf3))

- **ff-c**: C2 — schema-epoch catalog cache; zero catalog queries on steady-state CRUD
  ([#200](https://github.com/syn54x/ferro-orm/pull/200),
  [`8c6d6ed`](https://github.com/syn54x/ferro-orm/commit/8c6d6edf578cf55ed7ada0d49359a7e09528da1e))

- **ff-c**: C3+C4 — native typed Postgres decode; plan-driven enum hydration replaces _fix_types
  ([#198](https://github.com/syn54x/ferro-orm/pull/198),
  [`05a008c`](https://github.com/syn54x/ferro-orm/commit/05a008cb73f6c4dc8b2478a441c99282d07bd41d))

- **ff-d**: Session-scoped weak identity map with refresh-on-load; single-handle routing
  ([#201](https://github.com/syn54x/ferro-orm/pull/201),
  [`a328cfc`](https://github.com/syn54x/ferro-orm/commit/a328cfcd107e2cac998ddc8af50845857ec669a5))

- **ff-e**: Registry & model identity — qualified keys, configurable tables, O(N) import
  ([#209](https://github.com/syn54x/ferro-orm/pull/209),
  [`6391805`](https://github.com/syn54x/ferro-orm/commit/6391805c1b4ee46c0590513ac6684237c18f7430))

- **ff-f**: Query builder 1.0 shape — immutable chaining, build-time column validation, lambda-only
  predicates, QueryIR-only Rust ([#223](https://github.com/syn54x/ferro-orm/pull/223),
  [`eb7fece`](https://github.com/syn54x/ferro-orm/commit/eb7fece8fbc2231220ecf9111616f803f2f987eb))

- **ff-g-a**: Hardening — hydration ABI guard, transactional PG migrate, correctness edges,
  decode-path caching ([#232](https://github.com/syn54x/ferro-orm/pull/232),
  [`22617e1`](https://github.com/syn54x/ferro-orm/commit/22617e1cfe5e2bc9d0a3d7a1fed5acf4d8589cef))

### Refactoring

- **ff-g-b**: Operations.rs dedup — ModelMeta + Executor (G2)
  ([#233](https://github.com/syn54x/ferro-orm/pull/233),
  [`09c2c62`](https://github.com/syn54x/ferro-orm/commit/09c2c6214af9e2f9510d39d037935c53bd2e796d))

### Testing

- **ff-b**: B5 I-1 sentinel on the full backend matrix with a full-type fixture, zero filters
  ([`1141eb4`](https://github.com/syn54x/ferro-orm/commit/1141eb437b4843303e01556a5b24ddee9d0e0279))

- **ff-b**: Force psycopg v3 driver in the Postgres sentinel regardless of URL scheme
  ([`64f9845`](https://github.com/syn54x/ferro-orm/commit/64f98454bdb5e090a41bd214de06b7670974f4eb))


## v0.13.0 (2026-07-02)

### Bug Fixes

- **ir-p8.6**: Datetime/timestamptz coarseness — stop silent Postgres column reinterpretation (#154)
  ([#167](https://github.com/syn54x/ferro-orm/pull/167),
  [`7182a57`](https://github.com/syn54x/ferro-orm/commit/7182a5744900abe7ed50915edbca7d5184b2c535))

- **ir-p8.6**: Stop false-positive BLOB drift warning on SQLite (#165)
  ([#168](https://github.com/syn54x/ferro-orm/pull/168),
  [`d554ac4`](https://github.com/syn54x/ferro-orm/commit/d554ac4558286106bcb0908baec9fa3d49a2781d))

- **ir-p8.6**: Surface real PEP 649 deferred-annotation error (#155)
  ([#163](https://github.com/syn54x/ferro-orm/pull/163),
  [`00976e7`](https://github.com/syn54x/ferro-orm/commit/00976e748d86246b8be47230229d19516d8bb3c0))

### Documentation

- Use model-named lambda predicates in examples
  ([#132](https://github.com/syn54x/ferro-orm/pull/132),
  [`420ca6e`](https://github.com/syn54x/ferro-orm/commit/420ca6e1cb4a90d1acc556a83373c9da99b22bb3))

- **agents**: Add I-11 — explain concepts plainly, example-first
  ([`efc7ef5`](https://github.com/syn54x/ferro-orm/commit/efc7ef5678e2f6efacaa3a6e84c0575b0cdef51c))

- **ir-first**: Add Phase 8.6 post-8.5 cleanup backlog (epic #145)
  ([#147](https://github.com/syn54x/ferro-orm/pull/147),
  [`e57b74b`](https://github.com/syn54x/ferro-orm/commit/e57b74b397bd51ebc4809f25d57536896540b9de))

- **ir-first**: Expand #144 scope — auto-migrate index/unique reconciliation
  ([#150](https://github.com/syn54x/ferro-orm/pull/150),
  [`fdbac40`](https://github.com/syn54x/ferro-orm/commit/fdbac4003e9bd25fdc3f3cb0012c5dd9d562ee68))

- **ir-first**: Lowering-consolidation audit + Phase 8.5
  ([#138](https://github.com/syn54x/ferro-orm/pull/138),
  [`c6ce53c`](https://github.com/syn54x/ferro-orm/commit/c6ce53c2409a1fd20a40c1de58d42e1e3d2e1344))

- **ir-p8.6**: Add #153 create-path IR unification spec
  ([`be4bb99`](https://github.com/syn54x/ferro-orm/commit/be4bb99b90e6d02a6e01bfc5807a7cb178055862))

- **ir-p8.6**: Add #153 create-path unification implementation plan
  ([`de3c427`](https://github.com/syn54x/ferro-orm/commit/de3c42794de821d8e47eb5ca94b582f8199992df))

- **ir-p8.6**: Sync roadmap — #153 merged, file #158 (db_check check-renderer)
  ([`62c80fa`](https://github.com/syn54x/ferro-orm/commit/62c80fa3fe7ac456169d280f3ee8795feac32a8c))

- **ir-p8.6**: Sync roadmap — #154 merged (datetime/timestamptz warn-and-skip); #145 now 6/7
  ([`31dbbb6`](https://github.com/syn54x/ferro-orm/commit/31dbbb6c0f1d3afa0ed452ed9ae34441df69c501))

- **ir-p8.6**: Sync roadmap — #162 merged (typed save/update bind), file #165
  ([`5ca76a1`](https://github.com/syn54x/ferro-orm/commit/5ca76a1e177f4847ded4fb19402841a9988b62d1))

- **ir-p8.6**: Sync roadmap — #165 merged (BLOB introspection); #145 complete 7/7, Phase 8.6 wrapped
  ([`1dbb278`](https://github.com/syn54x/ferro-orm/commit/1dbb27864881d77a68d5026ab5a9bb07f142c3c5))

- **rust**: Add detailed docstrings for all public APIs
  ([#130](https://github.com/syn54x/ferro-orm/pull/130),
  [`8d5a951`](https://github.com/syn54x/ferro-orm/commit/8d5a95162568eef215ba03524a31b657df87e8c6))

### Features

- Ir cutover ([#137](https://github.com/syn54x/ferro-orm/pull/137),
  [`0509c2a`](https://github.com/syn54x/ferro-orm/commit/0509c2a35b0f0200492143a52563146359550129))

- **ir-p8.6**: Route save/update/bulk value bind through the typed codec path (#162)
  ([#166](https://github.com/syn54x/ferro-orm/pull/166),
  [`486e68c`](https://github.com/syn54x/ferro-orm/commit/486e68c110c03356fa579051849b44e8f4eae751))

### Refactoring

- Lowering consolidation & single-source-of-truth closeout (#139)
  ([#156](https://github.com/syn54x/ferro-orm/pull/156),
  [`f76bec9`](https://github.com/syn54x/ferro-orm/commit/f76bec93651bc9741bea85171da927ddb8864a1a))

- Unify the CREATE TABLE path onto the Python SchemaIR (#153)
  ([#157](https://github.com/syn54x/ferro-orm/pull/157),
  [`c9ce7b0`](https://github.com/syn54x/ferro-orm/commit/c9ce7b093fde350da8a1653cf7c42f522e0523b2))

- Unify the three dialect enums into one shared Dialect (#146)
  ([#159](https://github.com/syn54x/ferro-orm/pull/159),
  [`4b0dfe6`](https://github.com/syn54x/ferro-orm/commit/4b0dfe6b9eca1a2a63631a54504c629f42f7c5e1))

- **ir-p8.6**: Single check-renderer for db_check (#158)
  ([#161](https://github.com/syn54x/ferro-orm/pull/161),
  [`c04df1c`](https://github.com/syn54x/ferro-orm/commit/c04df1ce5aa4505f737864944855ff4170a4d350))


## v0.12.3 (2026-06-24)

### Bug Fixes

- **session**: Reject close while transactions are active
  ([#128](https://github.com/syn54x/ferro-orm/pull/128),
  [`e46f183`](https://github.com/syn54x/ferro-orm/commit/e46f183fba7a3bcbc635f7e66502a77aeb56861d))

- **session**: Serialize concurrent close teardown
  ([#129](https://github.com/syn54x/ferro-orm/pull/129),
  [`7afa616`](https://github.com/syn54x/ferro-orm/commit/7afa6168abd2eb19b564fc1c2ab9956e03a20fe9))


## v0.12.2 (2026-06-23)

### Bug Fixes

- Cross-context session teardown (Fixes #123) ([#127](https://github.com/syn54x/ferro-orm/pull/127),
  [`6b9ae13`](https://github.com/syn54x/ferro-orm/commit/6b9ae1332becef88f14bdb4beae1d24e3a4104cd))


## v0.12.1 (2026-06-23)

### Bug Fixes

- Stop framework predicate warnings and bind unnamed sessions to default
  ([#124](https://github.com/syn54x/ferro-orm/pull/124),
  [`66b92d8`](https://github.com/syn54x/ferro-orm/commit/66b92d8b691a1ad806691b9841bcbaf93c78f691))


## v0.12.0 (2026-06-23)

### Documentation

- Rewrite site on Zensical with runnable examples
  ([#70](https://github.com/syn54x/ferro-orm/pull/70),
  [`da86911`](https://github.com/syn54x/ferro-orm/commit/da869118c55314363139a021c139a52a2bc8d1fb))

### Features

- IR-first architecture program (Phases 0–7) ([#116](https://github.com/syn54x/ferro-orm/pull/116),
  [`1c22857`](https://github.com/syn54x/ferro-orm/commit/1c228577aa663c4630a5c8a33f0bb000ebe288b2))


## v0.11.0 (2026-06-11)

### Chores

- Solution quality requirements
  ([`c62770b`](https://github.com/syn54x/ferro-orm/commit/c62770b6d3073e8d596c30f29895bccddc2a4d7f))

### Documentation

- Compound SQLite hydration learnings (#56, #58)
  ([#60](https://github.com/syn54x/ferro-orm/pull/60),
  [`b609e72`](https://github.com/syn54x/ferro-orm/commit/b609e7274e1886cc5d8564c7adb52fc3c79ffc8d))

### Features

- Extend auto_migrate with column updates and destructive drops
  ([#69](https://github.com/syn54x/ferro-orm/pull/69),
  [`a76cb0f`](https://github.com/syn54x/ferro-orm/commit/a76cb0f2451dab79303353f2c77d74ca3e1ad4a5))


## v0.10.5 (2026-05-25)

### Bug Fixes

- Coerce Annotated StrEnum fields on cold hydration
  ([#66](https://github.com/syn54x/ferro-orm/pull/66),
  [`c17c13b`](https://github.com/syn54x/ferro-orm/commit/c17c13b56e100028a42fa431ca59c9729a22daeb))


## v0.10.4 (2026-05-24)

### Bug Fixes

- **query**: Cast native Postgres enum RHS in `.where()` filters
  ([#64](https://github.com/syn54x/ferro-orm/pull/64),
  [`7fea893`](https://github.com/syn54x/ferro-orm/commit/7fea89320fd2216a956ae32e8ae6f4829dd8fcf7))


## v0.10.3 (2026-05-21)

### Bug Fixes

- **query**: Typed predicates `col == None` / `!= None` → IS NULL / IS NOT NULL
  ([#62](https://github.com/syn54x/ferro-orm/pull/62),
  [`fd4b53e`](https://github.com/syn54x/ferro-orm/commit/fd4b53e26e01f8cf41a9b73de7c29b6901dee786))


## v0.10.2 (2026-05-19)

### Bug Fixes

- Hydrate SQLite INTEGER-backed Decimal columns on reconnect
  ([#59](https://github.com/syn54x/ferro-orm/pull/59),
  [`6f13906`](https://github.com/syn54x/ferro-orm/commit/6f13906850300bc9e85c1274763c49bc5b318b5d))


## v0.10.1 (2026-05-19)

### Bug Fixes

- **sqlite**: Hydrate SQL NULL as None instead of int 0
  ([#57](https://github.com/syn54x/ferro-orm/pull/57),
  [`249c81f`](https://github.com/syn54x/ferro-orm/commit/249c81f4b37f117c3ca80f44a9682511154ab9ec))

### Testing

- **schema**: Integration coverage for db_type / db_check
  ([#55](https://github.com/syn54x/ferro-orm/pull/55),
  [`b97d596`](https://github.com/syn54x/ferro-orm/commit/b97d59667d520c026d8a6fbb73ad1de7f71593b4))


## v0.10.0 (2026-05-18)

### Features

- Configurable column storage types (db_type / db_check)
  ([#53](https://github.com/syn54x/ferro-orm/pull/53),
  [`bd5feee`](https://github.com/syn54x/ferro-orm/commit/bd5feee970eda6031eca742ff18ab3a863d7abe4))


## v0.9.2 (2026-05-14)

### Bug Fixes

- **hydration**: Initialize Pydantic slots on Rust-hydrated models
  ([#51](https://github.com/syn54x/ferro-orm/pull/51),
  [`7609886`](https://github.com/syn54x/ferro-orm/commit/760988649bbfb41d1e46934cdea589efffdfa1b1))


## v0.9.1 (2026-05-11)

### Bug Fixes

- ModelConnection annotations
  ([`337b983`](https://github.com/syn54x/ferro-orm/commit/337b9838c18835b6e00bc20487b9c24fc76bfaeb))


## v0.9.0 (2026-05-09)

### Chores

- Gitignore .context and untrack committed artifacts
  ([#49](https://github.com/syn54x/ferro-orm/pull/49),
  [`cac6330`](https://github.com/syn54x/ferro-orm/commit/cac63304bbcf6bb3fad22037c33e6e60dcb8946e))

### Features

- Add get_or_none method
  ([`0c81e9f`](https://github.com/syn54x/ferro-orm/commit/0c81e9f414074c9a3631eb94f3a8077d16671e41))

### Testing

- Add tests for explicit shadow fields
  ([`78a3471`](https://github.com/syn54x/ferro-orm/commit/78a3471d55afec3b9b7da9f44e497ec521f7d1c0))


## v0.8.0 (2026-05-09)

### Features

- **query**: Typed query predicates via col() and lambda
  ([#48](https://github.com/syn54x/ferro-orm/pull/48),
  [`e34e3ca`](https://github.com/syn54x/ferro-orm/commit/e34e3ca43adfc851d757d8232d5736fb453db55e))


## v0.7.0 (2026-05-08)

### Features

- Per-connection identity_map on connect ([#47](https://github.com/syn54x/ferro-orm/pull/47),
  [`0a1d629`](https://github.com/syn54x/ferro-orm/commit/0a1d62926538cde14fdd4f4deece21a59a1ede69))


## v0.6.1 (2026-05-07)

### Refactoring

- Make ModelConnection generic to preserve model typing through .using()
  ([#46](https://github.com/syn54x/ferro-orm/pull/46),
  [`50d6b68`](https://github.com/syn54x/ferro-orm/commit/50d6b683059ce1d2b00942efd7267836db00eefd))


## v0.6.0 (2026-04-30)

### Features

- Support typed binds and named database routing
  ([#45](https://github.com/syn54x/ferro-orm/pull/45),
  [`e3fc930`](https://github.com/syn54x/ferro-orm/commit/e3fc9300178dce7ba763b744b92acf0385b9e90e))


## v0.5.0 (2026-04-28)

### Bug Fixes

- **ci**: Make cargo test link against libpython by gating extension-module
  ([`e3b013e`](https://github.com/syn54x/ferro-orm/commit/e3b013eeaa6ee96b676ecb0777539eb080c62238))

- **fk**: Address P2/P3 review findings ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`0ea6e02`](https://github.com/syn54x/ferro-orm/commit/0ea6e02588b319315ff354cf7facc04ab9e9eec9))

- **raw**: Make raw SQL tests pass on Postgres backend matrix
  ([#31](https://github.com/syn54x/ferro-orm/pull/31),
  [`7b3c5e6`](https://github.com/syn54x/ferro-orm/commit/7b3c5e62dd7a055121a64abd30f140635d04a78e))

- **schema**: Align Alembic single-column index names with Rust DDL
  ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`5e3211f`](https://github.com/syn54x/ferro-orm/commit/5e3211f30ba49d616f3428c1716cc26cfadf66ef))

### Chores

- Refresh uv.lock and persist code-review artifacts
  ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`54e2cad`](https://github.com/syn54x/ferro-orm/commit/54e2cade22278dda31454c7406b75c8b35ae27a4))

### Code Style

- Ruff format touched files
  ([`bbfda46`](https://github.com/syn54x/ferro-orm/commit/bbfda465c6cb51f103fe7407645284612172f132))

### Documentation

- Add AGENTS.md invariants and seed docs/solutions/
  ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`8b0af7f`](https://github.com/syn54x/ferro-orm/commit/8b0af7f85594397b7f53906fc4d49a4839fe6de9))

- **fk**: Document ForeignKey(index=True) and add CHANGELOG entry
  ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`625a3f2`](https://github.com/syn54x/ferro-orm/commit/625a3f2564fb7429085af0bf71ec441c2c571da9))

- **orm**: Document __ferro_composite_indexes__ and reverse_index
  ([`5ce3abe`](https://github.com/syn54x/ferro-orm/commit/5ce3abe00c767cb8fc3fa7bdb8b367cc3dec1a57))

- **raw**: Add raw SQL API page, guide section, CHANGELOG entry
  ([#31](https://github.com/syn54x/ferro-orm/pull/31),
  [`4b4699f`](https://github.com/syn54x/ferro-orm/commit/4b4699f618ef47f5b1add42b91e464a633ab0be8))

### Features

- **alembic**: Emit sa.Index for ferro_composite_indexes groups
  ([`a0c8176`](https://github.com/syn54x/ferro-orm/commit/a0c81761382bdd7ba537a8abc9198199e6b624ee))

- **fk**: Accept index kwarg on ForeignKey ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`ec39efe`](https://github.com/syn54x/ferro-orm/commit/ec39efec5ae135a8178bf216e1ea40ed16edd79c))

- **fk**: Propagate ForeignKey.index onto shadow column property
  ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`6d0f1f6`](https://github.com/syn54x/ferro-orm/commit/6d0f1f62e0bedc1f466db2ab9df6b8464551bd92))

- **fk**: Warn on redundant ForeignKey(unique=True, index=True)
  ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`3b311d7`](https://github.com/syn54x/ferro-orm/commit/3b311d70e35ffd3c589b877b57d3bfa7924cbb72))

- **orm**: Add __ferro_composite_indexes__ validation and schema injection
  ([`18f3b37`](https://github.com/syn54x/ferro-orm/commit/18f3b379b8e8fd08d57e47cb3c4e673feea84d7a))

- **raw**: Add ferro.execute/fetch_all/fetch_one with _marshal
  ([#31](https://github.com/syn54x/ferro-orm/pull/31),
  [`944df61`](https://github.com/syn54x/ferro-orm/commit/944df6142662949fcce3870ffaf84e0554b10514))

- **raw**: Add python_to_engine_bind_value helper for raw SQL binds
  ([`70bbbd3`](https://github.com/syn54x/ferro-orm/commit/70bbbd3cb7dc567dd4c7ea8bb754d883e9494cee))

- **raw**: Transaction() yields Transaction handle for tx-bound raw SQL
  ([#31](https://github.com/syn54x/ferro-orm/pull/31),
  [`226b575`](https://github.com/syn54x/ferro-orm/commit/226b5759676f56df46b9ef95e9f3491cbdce5c67))

- **raw**: Wire raw_execute/raw_fetch_all/raw_fetch_one through PyO3
  ([#31](https://github.com/syn54x/ferro-orm/pull/31),
  [`d31b580`](https://github.com/syn54x/ferro-orm/commit/d31b580002fb207379aa56fd80557939076f6032))

- **relations**: Add reverse_index opt-out for default M2M join tables
  ([`f1491df`](https://github.com/syn54x/ferro-orm/commit/f1491dfa9a583490da5563496cfc947dc34781ce))

- **rust**: Emit non-unique CREATE INDEX for ferro_composite_indexes
  ([`e8b2d06`](https://github.com/syn54x/ferro-orm/commit/e8b2d062ae050390274fb043fe84b1ebf4aed654))

### Testing

- Add cross-emitter DDL parity sentinel ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`e0ccc1a`](https://github.com/syn54x/ferro-orm/commit/e0ccc1a3be15a21ff80b06fe6b8dac6446882279))

- Red test for ForeignKey(index=True) shadow column index
  ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`2bbedc5`](https://github.com/syn54x/ferro-orm/commit/2bbedc5f8ff5a2ee8d615d3b3332fee40fe4560d))

- **fk**: Regression guards for FK index default and nullable interaction
  ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`8f6a48d`](https://github.com/syn54x/ferro-orm/commit/8f6a48d8877788ac36f6648e1bcd59dbf2a2c1ae))

- **fk**: Runtime DDL parity for ForeignKey(index=True)
  ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`99d39f3`](https://github.com/syn54x/ferro-orm/commit/99d39f33b2aa39715f3a9bda93694a211dda6992))

- **orm**: Cover common composite-index use cases
  ([`b53643f`](https://github.com/syn54x/ferro-orm/commit/b53643f48ce99b4587351272f94ac0ee17a46b94))

- **orm**: Cover composite indexes on Postgres catalog
  ([`13bc21f`](https://github.com/syn54x/ferro-orm/commit/13bc21fe05756eb5a05b9e5040b56692c0b6ceb3))

- **orm**: Cover composite indexes with UUID/enum columns and autogen idempotence
  ([`a2ec4a5`](https://github.com/syn54x/ferro-orm/commit/a2ec4a5bba322f2c3564797879dc7bbb795717f5))

- **orm**: Cover composite-index overlap with composite-uniques
  ([`24b7481`](https://github.com/syn54x/ferro-orm/commit/24b7481f64e9ee9d1b7f2dda9818b40925228a4d))

- **raw**: Cover active-tx ContextVar pickup for top-level execute
  ([#31](https://github.com/syn54x/ferro-orm/pull/31),
  [`3f7a2e4`](https://github.com/syn54x/ferro-orm/commit/3f7a2e44879b7a0baf55baebd1271fe03f79b767))

- **raw**: Cover fetch_all/fetch_one shape and read-your-writes
  ([#31](https://github.com/syn54x/ferro-orm/pull/31),
  [`8b4058c`](https://github.com/syn54x/ferro-orm/commit/8b4058c78baaad008c207570d4b71a8d9b8a0495))

- **raw**: Cover invalid-SQL surface and savepoint rollback
  ([#31](https://github.com/syn54x/ferro-orm/pull/31),
  [`a03a398`](https://github.com/syn54x/ferro-orm/commit/a03a3983a5dba298b4f4a64788152dd7dc552638))

- **raw**: Cover Postgres RLS set_config/current_setting use case
  ([#31](https://github.com/syn54x/ferro-orm/pull/31),
  [`4243123`](https://github.com/syn54x/ferro-orm/commit/424312380f0b56550ba538612060936cbc932d8f))

- **raw**: Cover UUID/datetime/Decimal/Enum/dict bind types
  ([#31](https://github.com/syn54x/ferro-orm/pull/31),
  [`217d8b4`](https://github.com/syn54x/ferro-orm/commit/217d8b4053a2cc254629e1e4867c24c9b24a2245))

- **relations**: Cover M2M reverse_index live catalog and edge cases
  ([`9aa9740`](https://github.com/syn54x/ferro-orm/commit/9aa97400a9885b876e86a0442ad4e3fe141f5d30))

- **rust**: Fix composite-index unit-test assertions for sea-query output
  ([`5c1ada1`](https://github.com/syn54x/ferro-orm/commit/5c1ada1ca975f07523c675c31933c643b0aa2cb5))

- **rust**: FK column with index flag still emits CREATE INDEX
  ([#32](https://github.com/syn54x/ferro-orm/pull/32),
  [`1eca573`](https://github.com/syn54x/ferro-orm/commit/1eca573af9066072e768a1854f119fecc785c102))


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
