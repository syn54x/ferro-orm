"""Runnable companion for the Upgrade guide's primary-key recipe
(docs/pages/upgrading.md).

A model with two ``primary_key=True`` fields is refused at class definition.
The assignment twin is ``upgrade_primary_key.py``.
"""

from typing import Annotated

from ferro import Field, Model

# --8<-- [start:refused]
try:

    class Account(Model):
        id: Annotated[int | None, Field(primary_key=True)] = None
        email: Annotated[str, Field(primary_key=True)]  # a second primary key

except TypeError as error:
    assert "declares multiple primary-key columns" in str(error)
else:
    raise AssertionError("a model with two primary keys was accepted")
# --8<-- [end:refused]
