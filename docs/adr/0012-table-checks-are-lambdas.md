# Table checks are named Check lambdas, not SQL

A table check is declared as `Check(suffix, predicate)` in `__ferro_checks__`. The predicate is a ferro lambda over that model's columns — the same dialect as `where()`, minus relation traversal, existence tests, and aggregates. The live constraint name is `ck_<table>_<suffix>` (63-char truncation as column checks); the suffix is an identifier, unique per model, and must not collide with a column check's full name. Raw SQL strings were rejected so both emitters render from one IR (I-1), unknown columns fail at class definition, and models stay dialect-free. SQL functions join later by expanding the predicate dialect, not by adding a second body language. A `Check` helper (not a raw `(name, lambda)` tuple) because the two slots mean different things; composites stay string tuples because their payload is only column names.

Decision by owner (2026-08-18), grilling #339.

Rejected alternatives:

- **Raw SQL strings** (`"col IS NULL OR other IS NULL"`): covers `char_length` on day one, but makes the model a SQL dialect, skips column validation, and turns cross-emitter parity into "copy the user's quoting."
- **Both SQL and lambdas**: two body languages to document, test, and keep in parity; the SQL door would be the one every example uses.
- **Bare `(name, lambda)` tuples**: matches composite ClassVars on the surface, but the two slots are different kinds and the type is a footgun.
- **Dict `{name: lambda}`**: duplicate names silently overwrite.
