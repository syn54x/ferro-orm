# A refusal's fix is a list of edits

```python
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team, on_delete="SET NULL")
    team: Relation[Team] = Forward("team_id")
```

```text
TypeError: Player.team_id: on_delete="SET NULL" needs a nullable column. Fix it by hand:
(1) on Player, replace team_id with `team_id: int | None = Field(default=None, foreign_key=Team, on_delete="SET NULL")`;
(2) on Player, replace team with `team: Relation[Team | None] = Forward("team_id")`
```

When Ferro refuses a model declaration, at class definition or when relations resolve, it computes the fix as a value: a tuple of `Edit`s, each naming a model, a field, an action (`replace`, `add` or `remove`) and the lines to write. The refusal is a `DeclarationRefusal`, a `TypeError` that carries those edits, and its message is `render_fix` of them. The test that proves a fix applies the same edits to the refused source and loads the result. The text a user reads and the fix the test ran are one value, so they cannot drift apart.

Whether a fix prints as a `Write:` line or as numbered steps follows from the edits, never from the refusal path: `Write:` only when every edit replaces the refused declaration. Without the `Forward`, the refusal above has one edit and prints `Write: team_id: int | None = ...`; with it, two declarations change and it prints steps. A refusal that cannot name a fix, because the reader must choose between models or nothing names the model at all, carries no edits and says what to do in words.

Edits are computed from the models in the registry. A fix that adds or changes a primary key takes the type and storage of the column that needs it, and every known foreign-key column the new key would not back gets an edit of its own, so following the fix loads every model.

Decided in Epic #874 (2026-11-08, Taylor), built in #898.

Rejected alternatives:

- **Text checked by hand-written test edits**: each refusal formatted its own message, and the sweep in #893 applied edits written out separately in the test. Five gate rounds on #893 each found a refusal whose printed fix did not work when followed, because nothing tied the message to the edits the test applied. Two of them, a `Write:` line for a `SET NULL` column a `Forward` reads and a primary-key step that always wrote `int`, shipped and moved here.
- **Each refusal path decides its locality**: a path chose between `Write:` and a recipe, so a fix that grew a second edit could keep printing `Write:`. Deriving it from the edits makes the rule hold by construction.

Consequences:

- `recipe()` and every hand-formatted `Write:` string are gone; `src/ferro/relations/fixes.py` holds the only renderer.
- A fix that adds a column on its own model, such as the key a `Forward` over a missing column needs, now prints a numbered step instead of `Write:`, since it adds a declaration rather than replacing the refused one.
- A refusal that offered alternatives (`Write: A or B`) names the choice in words and carries no edits.
- `tests/test_declaration_fixes.py` holds the sweep. A new refusal path adds a case there; the solutions page `docs/solutions/patterns/refusal-fixes.md` has the recipe.
