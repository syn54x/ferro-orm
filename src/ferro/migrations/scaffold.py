"""The skeleton of a hand-requested data step (``ferro migrate new --data-step``).

```text
$ ferro migrate new backfill_slugs --data-step Author
  0011_backfill_slugs/
    01_backfill_author.py
```

```python
from ferro.migrations import atomic, todo


@atomic
async def up(ctx):
    # ctx.models.Author is the table as this migration leaves it.
    todo("write this step")


@atomic
async def down(ctx):
    todo("write this step")
```

Both functions are unwritten steps until a person writes them: ``up``
refuses the migration at ``todo`` (ADR-0035). A project overrides the
skeleton with ``_templates/data_step.py`` beside its migrations, used
verbatim with ``{model}`` replaced by the model's class name.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "DATA_STEP_TEMPLATE",
    "PLACEHOLDERS",
    "TEMPLATES_DIR",
    "data_step",
    "step_name",
]

TEMPLATES_DIR = "_templates"
"""The directory beside the migrations that holds project templates (the
directory reader ignores ``_``-entries)."""

DATA_STEP_TEMPLATE = "data_step.py"

PLACEHOLDERS = ("{model}",)
"""What a template may say; each is replaced, nothing else is touched."""

_SKELETON = """\
from ferro.migrations import atomic, todo


@atomic
async def up(ctx):
    # ctx.models.{model} is the table as this migration leaves it.
    todo("write this step")


@atomic
async def down(ctx):
    todo("write this step")
"""


def step_name(model_name: str) -> str:
    """``backfill_author`` for ``Author``."""
    return f"backfill_{model_name.lower()}"


def data_step(model_name: str, *, template_dir: Path | None) -> str:
    """The text of a new data step over ``model_name``: the project's
    ``template_dir/data_step.py`` when it exists, else the skeleton, with
    ``{model}`` replaced."""
    template = _SKELETON
    if template_dir is not None:
        path = template_dir / DATA_STEP_TEMPLATE
        if path.is_file():
            template = path.read_bytes().decode("utf-8")
    return template.replace("{model}", model_name)
