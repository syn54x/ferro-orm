"""The ``ferro`` command's exit codes, shared by every verb (#473 decision 9).

Scripts and CI branch on these, so a value never changes meaning.
"""

OK = 0
"""The command did what it was asked, or there was nothing to do."""

REFUSED = 1
"""The command refused, naming the fix, or a step failed."""

USAGE = 2
"""The command was invoked wrongly (an unknown flag, a missing option) or
the ``cli`` extra is not installed."""

PENDING = 3
"""``status`` and ``check``: migrations are waiting to be applied."""

NEEDS_ATTENTION = 4
"""``status``, ``drift`` and ``baseline``: a failed or reverting step, an
edited file, or a database ahead of the checkout (``status``); drift between
the models and the database (``drift``, and ``baseline`` when the adopted
database drifts); a person has to look before anything runs."""
