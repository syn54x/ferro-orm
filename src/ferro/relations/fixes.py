"""A declaration refusal's fix, held as the edits that make it.

A refusal computes its fix as a list of `Edit`s from the actual models, and
`render_fix` prints the message from that list. The test that proves the fix
applies the same list, so the text a user reads is the fix the test ran. A
refusal the reader can fix in more than one way carries one list per way.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Edit:
    """One change to one declaration of a model's class body."""

    model: str
    """The model whose class body changes, by name."""
    field: str
    """The field the edit replaces, adds or removes; empty for a whole model
    the fix adds."""
    action: Literal["replace", "add", "remove"]
    """``replace`` the field's declaration, ``add`` lines to the class, or
    ``remove`` the field."""
    lines: tuple[str, ...] = ()
    """The declarations written, one line of source each; none for a removal."""


def _step(edit: Edit) -> str:
    """One numbered step of a fix that reaches beyond the refused declaration."""
    if not edit.field:
        return f"add the model `class {edit.model}(Model): {'; '.join(edit.lines)}`"
    if edit.action == "remove":
        return f"on {edit.model}, remove {edit.field}"
    written = " and ".join(f"`{line}`" for line in edit.lines)
    if edit.action == "replace":
        return f"on {edit.model}, replace {edit.field} with {written}"
    return f"on {edit.model}, add {written}"


def _ways(
    edits: Sequence[Edit], alternatives: Sequence[Sequence[Edit]]
) -> tuple[tuple[Edit, ...], ...]:
    """The ways to fix a refusal: ``alternatives``, or ``edits`` as the one way."""
    if alternatives:
        return tuple(tuple(way) for way in alternatives)
    return (tuple(edits),) if edits else ()


def render_fix(
    model: str,
    field: str,
    reason: str,
    edits: Sequence[Edit] = (),
    *,
    alternatives: Sequence[Sequence[Edit]] = (),
) -> str:
    """Write a refusal's message, its fix printed from its edits.

    A fix whose every edit replaces the refused declaration is local, and
    prints as ``Write: <line>``; several local ways print as
    ``Write: <line> or <line>``. Any other fix prints as numbered steps, each
    naming the model and field it changes, one list of steps per way. With
    no edits the message is the reason alone, which then says what to do in
    words.

    Args:
        model: The refused declaration's model.
        field: The refused declaration's field.
        reason: What is wrong, and for a refusal with no edits what to do.
        edits: The fix, in the order to apply it, when there is one way.
        alternatives: The ways to fix it, each in the order to apply it,
            when there are several; ``edits`` is then left empty.

    Returns:
        The message: ``<model>.<field>: <reason>``, then the fix.
    """
    message = f"{model}.{field}: {reason}"
    ways = _ways(edits, alternatives)
    if not ways:
        return message
    if all(
        edit.action == "replace" and (edit.model, edit.field) == (model, field)
        for way in ways
        for edit in way
    ):
        lines = " or ".join(
            " and ".join(line for edit in way for line in edit.lines) for way in ways
        )
        return f"{message}. Write: {lines}"
    steps = " Or: ".join(
        "; ".join(f"({index}) {_step(edit)}" for index, edit in enumerate(way, 1))
        for way in ways
    )
    return f"{message}. Fix it by hand: {steps}"


class DeclarationRefusal(TypeError):
    """A model declaration Ferro refuses, carrying the edits that fix it.

    Its message is `render_fix` of its own fields, so the printed fix and the
    edits never disagree.
    """

    def __init__(
        self,
        model: str,
        field: str,
        reason: str,
        edits: Sequence[Edit] = (),
        *,
        alternatives: Sequence[Sequence[Edit]] = (),
    ) -> None:
        self.model = model
        """The refused declaration's model."""
        self.field = field
        """The refused declaration's field."""
        self.reason = reason
        """What is wrong."""
        self.alternatives: tuple[tuple[Edit, ...], ...] = _ways(edits, alternatives)
        """Each way to fix it, in the order to apply it; empty when none can
        be named."""
        super().__init__(
            render_fix(model, field, reason, alternatives=self.alternatives)
        )

    @property
    def edits(self) -> tuple[Edit, ...]:
        """The fix when there is one way to fix it, else the first way."""
        return self.alternatives[0] if self.alternatives else ()

    def __reduce__(self) -> tuple:
        return _rebuilt, (self.model, self.field, self.reason, self.alternatives)


def _rebuilt(
    model: str, field: str, reason: str, alternatives: tuple[tuple[Edit, ...], ...]
) -> DeclarationRefusal:
    """A refusal rebuilt from its fields, for pickling."""
    return DeclarationRefusal(model, field, reason, alternatives=alternatives)
