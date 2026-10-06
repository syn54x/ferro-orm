"""The ``ferro`` command's exit codes, shared by every verb.

Scripts and CI branch on these, so a value never changes meaning.
"""

OK = 0
"""The command did what it was asked, or there was nothing to do."""

USAGE = 2
"""The command refused: a bad flag, or a refusal that names its fix."""

PENDING = 3
"""``status`` only: migrations are waiting to be applied."""

NEEDS_ATTENTION = 4
"""``status`` and ``drift``: a failed or reverting step, an edited file, or a
database ahead of the checkout; a person has to look before anything runs."""
