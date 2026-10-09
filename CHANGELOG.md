# CHANGELOG


## v0.22.0 (2026-10-09)

### Bug Fixes

- **ddl**: The check normalizer folds Postgres text casts
  ([#563](https://github.com/syn54x/ferro-orm/pull/563),
  [`2a79f82`](https://github.com/syn54x/ferro-orm/commit/2a79f820bae3a4df562fa0aaf4a53ef676387dc6))

- **migrate**: A migration with a data step drops in its contract, after the step
  ([#584](https://github.com/syn54x/ferro-orm/pull/584),
  [`482d386`](https://github.com/syn54x/ferro-orm/commit/482d386373c7e1092a29ae019a6c65d43d4f4f3d))

- **migrate**: A removed label's down keeps its place, a live label rename is only the rename, one
  copy of each plan helper ([#583](https://github.com/syn54x/ferro-orm/pull/583),
  [`8edd577`](https://github.com/syn54x/ferro-orm/commit/8edd57723c304ab4c780ff383ce03dd7ab50bc3d))

- **migrate**: Deepening close-out: a column's check rides its drop, the pass reports an enum type
  move, carried minors ([#604](https://github.com/syn54x/ferro-orm/pull/604),
  [`203c58a`](https://github.com/syn54x/ferro-orm/commit/203c58a9a743ab2572663a15dec1c177efd60e7e))

- **migrate**: Drift renders the planner's ChangePrimaryKey op
  ([#553](https://github.com/syn54x/ferro-orm/pull/553),
  [`c150d1b`](https://github.com/syn54x/ferro-orm/commit/c150d1b3eb0f3f314b9b3b2b6e6e5c25a68ae9b1))

- **migrate**: Drop dead DDL wrappers, bound --lock-timeout, one dialect parser
  ([#581](https://github.com/syn54x/ferro-orm/pull/581),
  [`8a53ed0`](https://github.com/syn54x/ferro-orm/commit/8a53ed0f031db8d56f3e83fdbc176813c9e2d6fc))

- **migrate**: Keep trigger and BEGIN ATOMIC bodies whole in SQL steps; probe label hints only under
  migrate_updates ([#580](https://github.com/syn54x/ferro-orm/pull/580),
  [`df671dd`](https://github.com/syn54x/ferro-orm/commit/df671dd43d71dd3feb8c12e91e805c2f19568cf1))

- **migrate**: Panel 2 generator fixes: no invented enum DEFAULT, AUTOINCREMENT kept across a
  rebuild, IndexOp gone (F15, F16, F21) ([#605](https://github.com/syn54x/ferro-orm/pull/605),
  [`244939b`](https://github.com/syn54x/ferro-orm/commit/244939b2edaa04c3fffde17152d71a3ff797685b))

- **migrate**: Panel 2 runtime findings (F17-F19, F25-F27)
  ([#607](https://github.com/syn54x/ferro-orm/pull/607),
  [`972b45e`](https://github.com/syn54x/ferro-orm/commit/972b45eaab81700a2ed7235ec2030053d2e30496))

- **migrate**: Same-storage columns are not drift on SQLite
  ([#556](https://github.com/syn54x/ferro-orm/pull/556),
  [`39be007`](https://github.com/syn54x/ferro-orm/commit/39be007b1250d9511b4ea18f4c602f7cd706d80c))

- **migrate**: Say migrations, not in-house migrations, in runtime messages
  ([#571](https://github.com/syn54x/ferro-orm/pull/571),
  [`e68a24f`](https://github.com/syn54x/ferro-orm/commit/e68a24f1bd30fd973fa6dd997abe35b09231c43e))

- **migrate**: Server defaults match on every door (SQLite backfill DEFAULT, renamed serial
  sequence) ([#608](https://github.com/syn54x/ferro-orm/pull/608),
  [`3ff5c08`](https://github.com/syn54x/ferro-orm/commit/3ff5c08dfed923f0d5877407b3ffdf737fe7f936))

- **migrate**: Status says applied, not installed, and pin the bridge's dropped-table filters
  ([#579](https://github.com/syn54x/ferro-orm/pull/579),
  [`673a77f`](https://github.com/syn54x/ferro-orm/commit/673a77f9095f21fdb404a3639b0fdd7679c5b1ce))

- **migrate**: The bridge downgrade marks a re-added required column, and a dropped model takes its
  enum type ([#574](https://github.com/syn54x/ferro-orm/pull/574),
  [`b2fe6e1`](https://github.com/syn54x/ferro-orm/commit/b2fe6e1e857d93c54c8d72c458f3c480e8916f59))

- **migrate**: The create pass refuses a declared table whose name a view holds
  ([#594](https://github.com/syn54x/ferro-orm/pull/594),
  [`76df6a1`](https://github.com/syn54x/ferro-orm/commit/76df6a1dd4ff5b10507475b7af25c2c9f511c46f))

- **migrate**: The pass renames a table whose live hint names a live table
  ([#573](https://github.com/syn54x/ferro-orm/pull/573),
  [`9165f78`](https://github.com/syn54x/ferro-orm/commit/9165f7838f4db3f71fbd6c57453472b4a9458ffc))

- **migrate**: The pass renders a renamed column's type change, and warns of rows a SQLite label
  rename strands ([#576](https://github.com/syn54x/ferro-orm/pull/576),
  [`278b496`](https://github.com/syn54x/ferro-orm/commit/278b496ebec69af3b08abc2a793d62bae10ae6db))

- **migrate**: The SQLite pass keeps NOT NULL DEFAULT; sequence rename refuses on a taken name
  (panel 2 N1, N2) ([#609](https://github.com/syn54x/ferro-orm/pull/609),
  [`a030cd4`](https://github.com/syn54x/ferro-orm/commit/a030cd47c32484b95994b7f17cfba39d8902f0ee))

- **migrations**: Panel 2 docs, public API and test hygiene (F20, F22–F24, F28, F29, render_plan)
  ([#606](https://github.com/syn54x/ferro-orm/pull/606),
  [`df84de1`](https://github.com/syn54x/ferro-orm/commit/df84de15a67712b49a37734bce61512d99fef043))

- **models**: Keep Any and dict | list refused as db_type json annotations
  ([#566](https://github.com/syn54x/ferro-orm/pull/566),
  [`f9eace3`](https://github.com/syn54x/ferro-orm/commit/f9eace30c5854424741ca9bad7e30db56aaf8d10))

- **tests**: Isolated_imports drops only a test's project modules
  ([#585](https://github.com/syn54x/ferro-orm/pull/585),
  [`9e154a5`](https://github.com/syn54x/ferro-orm/commit/9e154a528e2b70923654571faf1a700e04e9ee51))

- **transactions**: Roll back transaction() on BaseException (#484)
  ([#541](https://github.com/syn54x/ferro-orm/pull/541),
  [`e15e14b`](https://github.com/syn54x/ferro-orm/commit/e15e14b6998d7ac6c9f11720a62d4839569759f7))

### Documentation

- Close-out minors for epic #511 (contract rule, dated supersessions, ADR-0047 probe, bridge table
  drop) ([#586](https://github.com/syn54x/ferro-orm/pull/586),
  [`074f460`](https://github.com/syn54x/ferro-orm/commit/074f460fa1a16baac7ef7a7a4903f395e9f3eafc))

- **adr**: A plan's sides are adapters, one down function, every door plans index redefinition and
  foreign-key removal ([#590](https://github.com/syn54x/ferro-orm/pull/590),
  [`5415f96`](https://github.com/syn54x/ferro-orm/commit/5415f968cccb9f8bc8e06b7e2ca23443f6bf2f7f))

- **adr**: Record the epic #511 close-out decisions (pass rename hints, dropped-table drops, enum
  types, drops after data steps, applied) ([#578](https://github.com/syn54x/ferro-orm/pull/578),
  [`a0f57c3`](https://github.com/syn54x/ferro-orm/commit/a0f57c3e25f8c111ce7adffb0f821524558e8dbf))

- **adr**: The run object (ADR-0048) and the pass report (ADR-0049)
  ([#589](https://github.com/syn54x/ferro-orm/pull/589),
  [`3f5e5ce`](https://github.com/syn54x/ferro-orm/commit/3f5e5ceb1be513f4e2e9fdbfa4ac849f29b13c58))

- **agents**: Add the SDD routing block to AGENTS.md
  ([#512](https://github.com/syn54x/ferro-orm/pull/512),
  [`b5a9fda`](https://github.com/syn54x/ferro-orm/commit/b5a9fda910b958cf15a1c0b4f956647174a2f6a7))

- **context**: Data steps are atomic or chunked; ADR-0024 runner-owned chunking, ADR-0025 union
  snapshots ([#480](https://github.com/syn54x/ferro-orm/pull/480),
  [`19730e0`](https://github.com/syn54x/ferro-orm/commit/19730e09e17bacccd693c05ce686cbf66c26313d))

- **context**: Define Backfill, Expand step, Contract step and Restructure scaffold; ADR-0040 (#475)
  ([#500](https://github.com/syn54x/ferro-orm/pull/500),
  [`e2e7270`](https://github.com/syn54x/ferro-orm/commit/e2e72709cbe9d843e8d6c058396eb91a7799050a))

- **context**: Define Baseline; ADR-0031 (#467)
  ([#487](https://github.com/syn54x/ferro-orm/pull/487),
  [`82dd855`](https://github.com/syn54x/ferro-orm/commit/82dd855225b69f0bd03518f643b711c864ae416e))

- **context**: Define Database; ADR-0036 (#472)
  ([#494](https://github.com/syn54x/ferro-orm/pull/494),
  [`0bcee5b`](https://github.com/syn54x/ferro-orm/commit/0bcee5b20c6fe068b8832ba9f48ffbbc87d96a22))

- **context**: Define Down and Irreversible step; ADR-0033 (#469)
  ([#489](https://github.com/syn54x/ferro-orm/pull/489),
  [`ee94635`](https://github.com/syn54x/ferro-orm/commit/ee946356c82d104ae49e8a170b41c59fecbcd82b))

- **context**: Define Generated revision; ADR-0041 (#479)
  ([#501](https://github.com/syn54x/ferro-orm/pull/501),
  [`2e626da`](https://github.com/syn54x/ferro-orm/commit/2e626da5ebe64bf04c7dbd4e2abe053f39683607))

- **context**: Define Index step and DDL lock timeout; ADR-0044 (#505)
  ([#507](https://github.com/syn54x/ferro-orm/pull/507),
  [`2bdd868`](https://github.com/syn54x/ferro-orm/commit/2bdd86885f5654cc8a29a4447d302bc67c6dccb8))

- **context**: Define Migration test harness and Round trip; ADR-0045 (#506)
  ([#508](https://github.com/syn54x/ferro-orm/pull/508),
  [`f991337`](https://github.com/syn54x/ferro-orm/commit/f991337f317392ad5e0987a32a2483d8abd5be37))

- **context**: Define Migration, Step and Data step for the in-house migration system
  ([#476](https://github.com/syn54x/ferro-orm/pull/476),
  [`2f89231`](https://github.com/syn54x/ferro-orm/commit/2f892316c3e1874165a762b24c7c4298465a3cf9))

- **context**: Define Pending, Ahead and Project configuration; ADR-0038, ADR-0039 (#474)
  ([#497](https://github.com/syn54x/ferro-orm/pull/497),
  [`67d9630`](https://github.com/syn54x/ferro-orm/commit/67d963093f29efe4a10e4fac3698e954cc540292))

- **context**: Define Rename hint, Destructive step, Data-dependent step; ADR-0032 (#468)
  ([#488](https://github.com/syn54x/ferro-orm/pull/488),
  [`942536e`](https://github.com/syn54x/ferro-orm/commit/942536eb7bf7f73ffc8ffdb3ecf9c10221f59274))

- **context**: Define Rendering and Guard step; ADR-0037 (#473)
  ([#496](https://github.com/syn54x/ferro-orm/pull/496),
  [`33de692`](https://github.com/syn54x/ferro-orm/commit/33de6929357f340eafe4c3145746c17fabeb456d))

- **context**: Define Run and Run lock; ADR-0028, ADR-0029 (#465)
  ([#483](https://github.com/syn54x/ferro-orm/pull/483),
  [`d1419ee`](https://github.com/syn54x/ferro-orm/commit/d1419ee2001c808421a91ada9efe0bcaed9ff5f9))

- **context**: Define Schema snapshot and Drift; ADR-0023 snapshots outlive IR versions
  ([#478](https://github.com/syn54x/ferro-orm/pull/478),
  [`0f64fd0`](https://github.com/syn54x/ferro-orm/commit/0f64fd04186dec68351f3cdc912b59fdd0d710ba))

- **context**: Define Staged constraint and Validate step; ADR-0043 (#502)
  ([#504](https://github.com/syn54x/ferro-orm/pull/504),
  [`4984a26`](https://github.com/syn54x/ferro-orm/commit/4984a2682ffbfc6fca2e033e6aa1b46dad4a1984))

- **context**: Define Staged NOT NULL and Add-constraint step; ADR-0042 (#499)
  ([#503](https://github.com/syn54x/ferro-orm/pull/503),
  [`6e1ecf7`](https://github.com/syn54x/ferro-orm/commit/6e1ecf70e1a2da7f2810c79d1e4a1c8a625b9fd4))

- **context**: Define Step context and Unwritten step; ADR-0035 (#471)
  ([#493](https://github.com/syn54x/ferro-orm/pull/493),
  [`aff9b9f`](https://github.com/syn54x/ferro-orm/commit/aff9b9fe0f9747e6e4b4838120f9049251a0e5f6))

- **context**: Define Table rebuild; ADR-0034 (#470)
  ([#490](https://github.com/syn54x/ferro-orm/pull/490),
  [`aefd0cb`](https://github.com/syn54x/ferro-orm/commit/aefd0cb6a2e7be73fe793957282887a67df2d5d0))

- **context**: Define Tracking table and Step record; ADR-0030 (#466)
  ([#485](https://github.com/syn54x/ferro-orm/pull/485),
  [`95535fd`](https://github.com/syn54x/ferro-orm/commit/95535fdd839a956e0396eb289b1b2ac4913d2268))

- **context**: Per-dialect DDL renderings and the DDL-bearing diff (#481)
  ([#482](https://github.com/syn54x/ferro-orm/pull/482),
  [`4d5a850`](https://github.com/syn54x/ferro-orm/commit/4d5a850927d17637d83964c17618117c0d3e5d02))

- **context**: Place the SQLite table rebuild in its phase step; ADR-0046 (#509)
  ([#510](https://github.com/syn54x/ferro-orm/pull/510),
  [`6dfe137`](https://github.com/syn54x/ferro-orm/commit/6dfe137006b4eb3136f9bbc084c21b5947485c52))

- **research**: Primary-source findings for the in-house migrations map
  ([#477](https://github.com/syn54x/ferro-orm/pull/477),
  [`99f3df4`](https://github.com/syn54x/ferro-orm/commit/99f3df41f5e6586c1a6d03131917e2fbfb8d27e0))

- **schema**: Auto-update renames a hinted table (#573)
  ([#575](https://github.com/syn54x/ferro-orm/pull/575),
  [`e007d86`](https://github.com/syn54x/ferro-orm/commit/e007d86a0caa510035d8584ff10eb5f3da6d12f4))

- **schema**: The Schema Management group around the two doors (#539)
  ([#570](https://github.com/syn54x/ferro-orm/pull/570),
  [`1cc9865`](https://github.com/syn54x/ferro-orm/commit/1cc986546819cc98bda01bf76312362ce80c0033))

### Features

- In-process migration API and the connect() auto-migrate guard (#521)
  ([#549](https://github.com/syn54x/ferro-orm/pull/549),
  [`5836929`](https://github.com/syn54x/ferro-orm/commit/58369297f57ff73a09c8a0f55de87835bd84e7ec))

- **cli**: Ferro console script, the cli extra and migrate init (#516)
  ([#545](https://github.com/syn54x/ferro-orm/pull/545),
  [`1312316`](https://github.com/syn54x/ferro-orm/commit/1312316a08d55f7a48b7ee98ca3c2da1038eed62))

- **migrate**: Chunked data steps: keyset batches, cursor and resume (#532)
  ([#562](https://github.com/syn54x/ferro-orm/pull/562),
  [`4ff736d`](https://github.com/syn54x/ferro-orm/commit/4ff736d328761a13faaaa77aebe063dd97844967))

- **migrate**: Column add/drop/type/nullability and indexes on existing tables (#524)
  ([#550](https://github.com/syn54x/ferro-orm/pull/550),
  [`8f5293e`](https://github.com/syn54x/ferro-orm/commit/8f5293ee074809003a848c980ce0ccf12cf6a41d))

- **migrate**: Data steps, declarations, step context, historical models and atomic steps (#530)
  ([#560](https://github.com/syn54x/ferro-orm/pull/560),
  [`24660d1`](https://github.com/syn54x/ferro-orm/commit/24660d16d76a50c68cdc45bbfeec9fc144b8f0d2))

- **migrate**: DDL lock timeout for runs and the reconciliation pass (#522)
  ([#552](https://github.com/syn54x/ferro-orm/pull/552),
  [`fb45464`](https://github.com/syn54x/ferro-orm/commit/fb4546414ff4be8d10e8267e9f1bb30cd9b6b9fe))

- **migrate**: Enum label removal and the primary-key refusal (#536)
  ([#569](https://github.com/syn54x/ferro-orm/pull/569),
  [`27f93e6`](https://github.com/syn54x/ferro-orm/commit/27f93e62d13a3cd7a910a59ca897eb6350be1d5c))

- **migrate**: Expand, backfill and contract for values existing rows need (#534)
  ([#565](https://github.com/syn54x/ferro-orm/pull/565),
  [`7990097`](https://github.com/syn54x/ferro-orm/commit/799009703c61609c74215ea693a6def7f2557366))

- **migrate**: Ferro migrate drift and ferro.migrations.drift() (#523)
  ([#551](https://github.com/syn54x/ferro-orm/pull/551),
  [`1a708c7`](https://github.com/syn54x/ferro-orm/commit/1a708c741513facd7aecc3f4596b7c3ff2593037))

- **migrate**: Ferro migrate new and check for new and dropped models (#518)
  ([#546](https://github.com/syn54x/ferro-orm/pull/546),
  [`906ad05`](https://github.com/syn54x/ferro-orm/commit/906ad05b300c3978a8becf9991f68e1fd7a58986))

- **migrate**: Ferro.migrations.testing, the migration test harness (#535)
  ([#561](https://github.com/syn54x/ferro-orm/pull/561),
  [`f8ca673`](https://github.com/syn54x/ferro-orm/commit/f8ca67301157bfb307c29ff11795309699c72e19))

- **migrate**: Generate enum label additions, label and type renames (#529)
  ([#559](https://github.com/syn54x/ferro-orm/pull/559),
  [`4b5dbf0`](https://github.com/syn54x/ferro-orm/commit/4b5dbf0ba82292f79d89c6ce22608910231ecebe))

- **migrate**: Migrate baseline (#525) ([#554](https://github.com/syn54x/ferro-orm/pull/554),
  [`c4c2ae5`](https://github.com/syn54x/ferro-orm/commit/c4c2ae5fa6154ac2051e1331dc794cb8d43700d3))

- **migrate**: Migrate down and generated downs (#520)
  ([#548](https://github.com/syn54x/ferro-orm/pull/548),
  [`bbbad98`](https://github.com/syn54x/ferro-orm/commit/bbbad98a821f70c3d47ab378a77f895db63a5af0))

- **migrate**: Postgres staged constraints, validate step and concurrent index steps (#527)
  ([#557](https://github.com/syn54x/ferro-orm/pull/557),
  [`c5a8cd8`](https://github.com/syn54x/ferro-orm/commit/c5a8cd8727379d2bd1b10c188c542919e8a0951a))

- **migrate**: Rename hints for columns, foreign keys and tables (#528)
  ([#558](https://github.com/syn54x/ferro-orm/pull/558),
  [`00bfc97`](https://github.com/syn54x/ferro-orm/commit/00bfc97c2530cf133e4d851a8d91d2bd02e4f8a3))

- **migrate**: Rerecord an edited step; refuse an edited chunked step until continued or restarted
  (#537) ([#568](https://github.com/syn54x/ferro-orm/pull/568),
  [`d3cec17`](https://github.com/syn54x/ferro-orm/commit/d3cec17442ae6c53cd6414e661870f87be27509d))

- **migrate**: Riders, redefined indexes, removed foreign keys
  ([#597](https://github.com/syn54x/ferro-orm/pull/597),
  [`cd53d6e`](https://github.com/syn54x/ferro-orm/commit/cd53d6e2d071c0df06351808b642249945e7b719))

- **migrate**: Row security in generated migrations (#531)
  ([#564](https://github.com/syn54x/ferro-orm/pull/564),
  [`9d21a88`](https://github.com/syn54x/ferro-orm/commit/9d21a883790b745e2e2c4341203c921745a0d035))

- **migrate**: Run planner, tracking table and run lock: migrate up and migrate status (#519)
  ([#547](https://github.com/syn54x/ferro-orm/pull/547),
  [`ad219f3`](https://github.com/syn54x/ferro-orm/commit/ad219f3d3c02dd0e7fd008cf085340e1b4249c99))

- **migrate**: SQLite inline db_check and ADD COLUMN REFERENCES; warnings name ferro migrate new
  (#514) ([#542](https://github.com/syn54x/ferro-orm/pull/542),
  [`283910e`](https://github.com/syn54x/ferro-orm/commit/283910ed25adad7294da03e97a91ce12fe580b84))

- **migrate**: SQLite table rebuild in its phase step (#526)
  ([#555](https://github.com/syn54x/ferro-orm/pull/555),
  [`0a07ae9`](https://github.com/syn54x/ferro-orm/commit/0a07ae99c3b0d2700d7a8a06a61bf523ca21eea9))

- **migrate**: The Alembic bridge translates the one planner (#533)
  ([#567](https://github.com/syn54x/ferro-orm/pull/567),
  [`9979b62`](https://github.com/syn54x/ferro-orm/commit/9979b622e666352253b13caced56d4cd308d5a84))

- **migrate**: Validate NOT VALID constraints and rebuild invalid indexes (#515)
  ([#543](https://github.com/syn54x/ferro-orm/pull/543),
  [`e0671a8`](https://github.com/syn54x/ferro-orm/commit/e0671a8ea7efbb64f159c41780cde7665527d723))

- **settings**: Project configuration through FerroSettings and model discovery (#513)
  ([#540](https://github.com/syn54x/ferro-orm/pull/540),
  [`bc74bf1`](https://github.com/syn54x/ferro-orm/commit/bc74bf1bf91849b103c78989244849e0b4294f28))

### Refactoring

- **migrate**: A run is one Rust object holding the lock, the directory read and the plan
  ([#596](https://github.com/syn54x/ferro-orm/pull/596),
  [`3183cbe`](https://github.com/syn54x/ferro-orm/commit/3183cbe1b7247af8dcf3c65d7c03e4da7a36ed19))

- **migrate**: One live read decides which tables every door reads
  ([#593](https://github.com/syn54x/ferro-orm/pull/593),
  [`d983625`](https://github.com/syn54x/ferro-orm/commit/d983625a513835d15807ecce24e3badd324a099e))

- **migrate**: One module renders a data step's text from the generator's record
  ([#588](https://github.com/syn54x/ferro-orm/pull/588),
  [`49e0922`](https://github.com/syn54x/ferro-orm/commit/49e0922c8cd1ee22eb4f86b405db531eeaf97f78))

- **migrate**: One planner over two snapshots (#517)
  ([#544](https://github.com/syn54x/ferro-orm/pull/544),
  [`4310e79`](https://github.com/syn54x/ferro-orm/commit/4310e79926faa2f40a8a7ea872c6a7c1a9ef77e0))

- **migrate**: One reading of where a database stands, one drift against a snapshot
  ([#587](https://github.com/syn54x/ferro-orm/pull/587),
  [`58d3d89`](https://github.com/syn54x/ferro-orm/commit/58d3d8963ca941891225b2727a12c012d0e464bd))

- **migrate**: Plan sides are adapters, one verdict per op, one down for every door
  ([#600](https://github.com/syn54x/ferro-orm/pull/600),
  [`989d17d`](https://github.com/syn54x/ferro-orm/commit/989d17dcd9786e6d574e48f554d5bac8045cf02b))

- **migrate**: Retire the per-artifact planner wrappers, pin through plan_from_ir
  ([#591](https://github.com/syn54x/ferro-orm/pull/591),
  [`49ccc87`](https://github.com/syn54x/ferro-orm/commit/49ccc87eae5227568dd6b345997737049ae5c5b2))

- **migrate**: The auto-migrate pass reports what it executed (A-1)
  ([#599](https://github.com/syn54x/ferro-orm/pull/599),
  [`a02bd7e`](https://github.com/syn54x/ferro-orm/commit/a02bd7e562d7e709379b583aadbed1fdb5051989))

- **migrate**: The DDL executor runs statement lists (and the create pass runs unprepared)
  ([#592](https://github.com/syn54x/ferro-orm/pull/592),
  [`8766011`](https://github.com/syn54x/ferro-orm/commit/8766011141c828f63af0074b6598b1272cf79638))

- **migrate**: The generator plans once and lays out every step in Rust (B5)
  ([#603](https://github.com/syn54x/ferro-orm/pull/603),
  [`40fb7ab`](https://github.com/syn54x/ferro-orm/commit/40fb7abf350bd132ef967e32cb29e84cf95f8515))

- **migrate**: Typed plan reports, and a plan that holds its sides
  ([#595](https://github.com/syn54x/ferro-orm/pull/595),
  [`c520346`](https://github.com/syn54x/ferro-orm/commit/c520346e2c7e4a2943c765267e3dfe4aaae8861f))

- **migrations**: One function per verb, resolved by one Target
  ([#598](https://github.com/syn54x/ferro-orm/pull/598),
  [`9109ff2`](https://github.com/syn54x/ferro-orm/commit/9109ff204ec0ef4e9665ceab15eb86c5d2ef30a1))

- **migrations**: The Alembic bridge's revision in one core call (B4)
  ([#602](https://github.com/syn54x/ferro-orm/pull/602),
  [`0d1d417`](https://github.com/syn54x/ferro-orm/commit/0d1d41787d654500566273974e7aeffd44534ec1))

### Testing

- Pin the migrations door as an I-1 emitter and rewrite AGENTS.md I-1 (#538)
  ([#572](https://github.com/syn54x/ferro-orm/pull/572),
  [`ae14b7b`](https://github.com/syn54x/ferro-orm/commit/ae14b7b3e65ac20289ab9a5c56b25c4db686fe5d))

- **migrate**: Pin the transitive hold-back of tables awaiting a rename
  ([#582](https://github.com/syn54x/ferro-orm/pull/582),
  [`939b37a`](https://github.com/syn54x/ferro-orm/commit/939b37a41436387696128eb744da445b160fa0cf))

- **parity**: Match the tracked-database refusal as #571 words it
  ([`cd04e46`](https://github.com/syn54x/ferro-orm/commit/cd04e469940dda312537b591e91d602625a655ff))

- **postgres**: Isolate concurrent suite runs on one server, and fix the guard's neighbour-schema
  race ([#601](https://github.com/syn54x/ferro-orm/pull/601),
  [`bd9c84a`](https://github.com/syn54x/ferro-orm/commit/bd9c84a72c7c0217e9dc96dea391d98a599b08a8))


## v0.21.2 (2026-09-29)

### Bug Fixes

- **alembic**: Create add_column-only enum types, type render_item, close the label-addition block
  ([#448](https://github.com/syn54x/ferro-orm/pull/448),
  [`dc49156`](https://github.com/syn54x/ferro-orm/commit/dc49156be6b8c998fd726230bc5a1ffb5fce15c4))


## v0.21.1 (2026-09-28)

### Bug Fixes

- **alembic**: Drop enum types in the downgrade of the create_table that made them
  ([#441](https://github.com/syn54x/ferro-orm/pull/441),
  [`5c86b7f`](https://github.com/syn54x/ferro-orm/commit/5c86b7fef03828443fd61852caa3705849e3150f))

- **alembic**: Render create_type=False for enum types a revision reuses
  ([#445](https://github.com/syn54x/ferro-orm/pull/445),
  [`6776d0b`](https://github.com/syn54x/ferro-orm/commit/6776d0b7b13499d85666280a623f91fbf2df6387))

- **checks**: Compare associative OR/AND chains flat in the drift normalizer
  ([#442](https://github.com/syn54x/ferro-orm/pull/442),
  [`4df72a2`](https://github.com/syn54x/ferro-orm/commit/4df72a2d37b456d79ba1fc9cfa6b5ba2a0c6f194))

### Documentation

- Replace Pinch RLS examples and humanize guide prose
  ([#433](https://github.com/syn54x/ferro-orm/pull/433),
  [`4471d75`](https://github.com/syn54x/ferro-orm/commit/4471d7510f7bf6b642f13e5f768ac723bbdb2288))

- Unpack dense Why Ferro, query, and RLS prose
  ([#434](https://github.com/syn54x/ferro-orm/pull/434),
  [`1af08af`](https://github.com/syn54x/ferro-orm/commit/1af08af1288d670139fa5f0beab19bf7b19d8c8c))

### Refactoring

- **alembic**: Enum type drop cleanups from the #441 review panel
  ([#444](https://github.com/syn54x/ferro-orm/pull/444),
  [`ae6b6d3`](https://github.com/syn54x/ferro-orm/commit/ae6b6d387d40727a79cb0f8a96c5151daffa1dbb))


## v0.21.0 (2026-09-17)

### Bug Fixes

- **alembic**: Emit check constraints after table ops
  ([#431](https://github.com/syn54x/ferro-orm/pull/431),
  [`fca2434`](https://github.com/syn54x/ferro-orm/commit/fca2434884cd3189096cc9503411cb7ffd1a5551))

- **query**: Canonicalize datetime/UUID/Decimal literals against save()
  ([#432](https://github.com/syn54x/ferro-orm/pull/432),
  [`23c4031`](https://github.com/syn54x/ferro-orm/commit/23c40319be8d590f00c54eea3efc9e02876f4b87))

- **query**: Session-aware merge dialect and recipe-column kwargs
  ([#385](https://github.com/syn54x/ferro-orm/pull/385),
  [`961e1d8`](https://github.com/syn54x/ferro-orm/commit/961e1d8411044a5e1667d9962d7a7cf3c08cef56))

### Features

- Postgres Row-Level Security — session settings + declarative row policies (PRD #406)
  ([#426](https://github.com/syn54x/ferro-orm/pull/426),
  [`c5d72f8`](https://github.com/syn54x/ferro-orm/commit/c5d72f831cb61e458d9f307fcd0745f1dfc03b4e))

- **migrate**: Accept JSON object/array literals on NOT NULL column adds
  ([#374](https://github.com/syn54x/ferro-orm/pull/374),
  [`aa55c16`](https://github.com/syn54x/ferro-orm/commit/aa55c16c2bbb79f470b93a134bd83df9ad540d2e))

- **query**: Position paging after()/before() from order_by
  ([#404](https://github.com/syn54x/ferro-orm/pull/404),
  [`de92b42`](https://github.com/syn54x/ferro-orm/commit/de92b424112eda30504c5ab75e8ebd64a7e239da))

- **query**: Value expressions in update recipes
  ([#384](https://github.com/syn54x/ferro-orm/pull/384),
  [`dc6087a`](https://github.com/syn54x/ferro-orm/commit/dc6087a3e249e754d4cce43bbe34b5f478f0df69))

- **save**: Partial persist with only= and exclude=
  ([#391](https://github.com/syn54x/ferro-orm/pull/391),
  [`e5e4692`](https://github.com/syn54x/ferro-orm/commit/e5e469232911e89886881732810ca52cbef5b0ab))


## v0.20.0 (2026-08-29)

### Chores

- Add Context7 library config ([#357](https://github.com/syn54x/ferro-orm/pull/357),
  [`ab8e79b`](https://github.com/syn54x/ferro-orm/commit/ab8e79b799e07fd64f7da8db09daa278af488877))

### Features

- **query**: Order_by nulls= first/last placement
  ([#369](https://github.com/syn54x/ferro-orm/pull/369),
  [`ef6c8b4`](https://github.com/syn54x/ferro-orm/commit/ef6c8b4c458d47a88ace4407a5fb368a8d91200a))


## v0.19.0 (2026-08-18)

### Bug Fixes

- Classify PG18 RESTRICT (23001) as ForeignKeyViolationError
  ([#338](https://github.com/syn54x/ferro-orm/pull/338),
  [`4fa1153`](https://github.com/syn54x/ferro-orm/commit/4fa1153477802de8207c7987748db962b35c2f0f))

### Features

- Named table-level CHECK constraints ([#356](https://github.com/syn54x/ferro-orm/pull/356),
  [`ba179e7`](https://github.com/syn54x/ferro-orm/commit/ba179e71e9bce4ab77a6fdbc1831661485de6799))


## v0.18.0 (2026-08-05)

### Documentation

- **adr**: ADR-0011 label addition + glossary terms (enum label, label addition)
  ([`449616f`](https://github.com/syn54x/ferro-orm/commit/449616f07aefe473fe098f45f6873eb2003cf8e5))

- **migrate**: Label addition — the auto_migrate trap, the migrate_updates contract, the ordering
  caveat ([#334](https://github.com/syn54x/ferro-orm/pull/334),
  [`482911b`](https://github.com/syn54x/ferro-orm/commit/482911b441eeeed21b13d3927c9492797bb1eba5))

### Features

- **alembic**: Autogenerate comparator emits label additions from the shared diff
  ([#333](https://github.com/syn54x/ferro-orm/pull/333),
  [`1579fc3`](https://github.com/syn54x/ferro-orm/commit/1579fc38a7f0d2b94c4ea51aa7daac652df30abc))

- **migrate**: Label addition tracer — migrate_updates appends missing enum labels
  ([#330](https://github.com/syn54x/ferro-orm/pull/330),
  [`e929a6a`](https://github.com/syn54x/ferro-orm/commit/e929a6ae854fd435c6aac3277e476c15b106f73a))

- **migrate**: Warn-never-act for extra enum labels; create pass stays silent
  ([#331](https://github.com/syn54x/ferro-orm/pull/331),
  [`a73d142`](https://github.com/syn54x/ferro-orm/commit/a73d142e2ff22ef8e47c2a8b8c34ca92372d0487))

### Testing

- **migrate**: Label addition edges — shared types, default-in-same-run, ordering, idempotence
  ([#332](https://github.com/syn54x/ferro-orm/pull/332),
  [`1215f0c`](https://github.com/syn54x/ferro-orm/commit/1215f0c0e52367eafd91411b7b7891220edf318e))


## v0.17.1 (2026-07-19)

### Bug Fixes

- **migrate**: Auto-migrate pass ownership — column-before-index sequencing and FK on_delete
  reconciliation ([#326](https://github.com/syn54x/ferro-orm/pull/326),
  [`3451b1d`](https://github.com/syn54x/ferro-orm/commit/3451b1dad37d8ac39709d986aa20b1de4c12542a))


## v0.17.0 (2026-07-18)

### Documentation

- Design existence tests (.exists()) and uniform ~ negation
  ([`c68a477`](https://github.com/syn54x/ferro-orm/commit/c68a47780cd5fe6d8ecfcb4f7e0f9c8ec2b798fe))

- **query**: Document universal ~ negation and three-valued logic
  ([`6061d66`](https://github.com/syn54x/ferro-orm/commit/6061d66e96c99f089339f7a4a83b9424101da8be))

### Features

- **query**: Bare .exists() on reverse FK — existence-test tracer bullet
  ([#319](https://github.com/syn54x/ferro-orm/pull/319),
  [`2538e06`](https://github.com/syn54x/ferro-orm/commit/2538e06b217e449d10dd564687f5ca3581d5d08d))

- **query**: Existence-test error surfaces and docs — the feature's edges
  ([`be88aed`](https://github.com/syn54x/ferro-orm/commit/be88aed35f0975f9333bf0dabacaffc8a82f8f34))

- **query**: M2M existence tests — the two-hop correlation path
  ([`336b529`](https://github.com/syn54x/ferro-orm/commit/336b5298d0e7beca8231452655544fcc7a15ad6a))

- **query**: Scoped existence tests — inner predicates, nesting, cross-scope guard
  ([`df11625`](https://github.com/syn54x/ferro-orm/commit/df11625b90e01d293f4863bec8afafb9110a1ac3))

- **query**: Uniform predicate negation — prefix ~ as a NOT wire node
  ([`bfa3126`](https://github.com/syn54x/ferro-orm/commit/bfa3126914b5a1b5d93a65ee3fe2f066bb314b1f))


## v0.16.3 (2026-07-16)

### Bug Fixes

- Derive the primary-key fact once and reject multi-PK models at class definition
  ([`ef91fa0`](https://github.com/syn54x/ferro-orm/commit/ef91fa09e09d5e7444e8d659b831921d40bcb365))

### Refactoring

- Compile_query returns one CompiledQuery artifact carrying the hop-class map
  ([`fc7084c`](https://github.com/syn54x/ferro-orm/commit/fc7084c4ad89b135815f74852263989f23868143))

- Single Registry owner for Python-side registration state
  ([`1179fcc`](https://github.com/syn54x/ferro-orm/commit/1179fcc07cc9e4305b7e7a8c141b8b29264dcd26))


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
