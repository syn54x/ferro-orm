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

Whether a fix prints as a `Write:` line or as numbered steps follows from the edits, never from the refusal path: `Write:` only when every edit replaces the refused declaration. Without the `Forward`, the refusal above has one edit and prints `Write: team_id: int | None = ...`; with it, two declarations change and it prints steps.

Some refusals can be fixed more than one way: a bare `players: Relation = Reverse("team_id")` that both `Player` and `Coach` point back to reads either. Such a refusal carries `alternatives`, one tuple of edits per way, and prints `Write: <line> or <line>` when every way is local. Only a refusal that can name no fix at all, because nothing names the model it reads, carries none and says what to do in words.

Edits are computed from the models in the registry. A fix that adds or changes a primary key takes the type and storage of the column that needs it, and every known foreign-key column the new key would not back gets an edit of its own, so following the fix loads every model. A fix that names a join table picks one no model, join table or other many-to-many holds, and resolution refuses a second many-to-many that declares a join table another one already does, since the two would share their rows.

A check that runs at two times raises one refusal from one input. A key column whose target declares no primary key is met by the schema compile when the target is written as a class, and by relation resolution when it is a name; both raise `keyless_target_refusal`, and both read the models that exist at that moment, the model being defined included: at class definition its column specs are handed in before it publishes them, so its other key columns get their edits either way.

Decided in Epic #874 (2026-11-08, Taylor), built in #898.

Rejected alternatives:

- **Text checked by hand-written test edits**: each refusal formatted its own message, and the sweep in #893 applied edits written out separately in the test. Five gate rounds on #893 each found a refusal whose printed fix did not work when followed, because nothing tied the message to the edits the test applied. Two of them, a `Write:` line for a `SET NULL` column a `Forward` reads and a primary-key step that always wrote `int`, shipped and moved here.
- **Each refusal path decides its locality**: a path chose between `Write:` and a recipe, so a fix that grew a second edit could keep printing `Write:`. Deriving it from the edits makes the rule hold by construction.

Consequences:

- `recipe()` and every hand-formatted `Write:` string are gone; `src/ferro/relations/fixes.py` holds the only renderer.
- A fix that adds a column on its own model, such as the key a `Forward` over a missing column needs, now prints a numbered step instead of `Write:`, since it adds a declaration rather than replacing the refused one.
- A refusal that offers several ways (`Write: A or B`) carries each way as its own list of edits, and the sweep applies each one.
- `tests/test_declaration_fixes.py` holds the sweep, which applies each way to fix each refusal and loads the models. Every case whose target can be written both as a class and as a name runs both ways, and the two refusals must carry the same edits. `test_the_sweep_reaches_every_refusal` finds every call in `src/ferro` that builds a `DeclarationRefusal`, directly or through a function returning one, and requires each to be on the stack of a refusal the sweep meets, so a new refusal without a sweep case fails the suite however it is raised. The solutions page `docs/solutions/patterns/refusal-fixes.md` has the recipe.
- A redefined model (a notebook re-running a cell) releases the join tables its old definition declared, so its many-to-manys declare them again instead of being refused against a stale entry.
- A refusal raised when a class is defined cannot see models defined after it. Its fix is complete for the models that exist; a later model whose key no longer fits the new primary key is refused in turn, with its own fix.
