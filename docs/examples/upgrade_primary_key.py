"""Runnable companion for the Upgrade guide's primary-key recipe
(docs/pages/upgrading.md).

A model with two ``primary_key=True`` fields is refused at class definition.
The Annotated twin is ``upgrade_primary_key_annotated.py``.
"""

from ferro import Field, Model

# --8<-- [start:refused]
try:

    class Account(Model):
        id: int | None = Field(default=None, primary_key=True)
        email: str = Field(primary_key=True)  # a second primary key

except TypeError as error:
    assert "declares multiple primary-key columns" in str(error)
else:
    raise AssertionError("a model with two primary keys was accepted")
# --8<-- [end:refused]
