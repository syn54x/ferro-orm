"""FF-D D5: relationship inputs must read the *target* model's PK field.

Today `Model.__init__` scans the source class's ferro_fields for the PK name
and reads that name off the target instance — correct only while every PK is
named `id`.
"""

from typing import Annotated

from ferro import Field, Model
from ferro.base import ForeignKey


def test_fk_extraction_uses_target_pk_name():
    class D5Warehouse(Model):
        code: int | None = Field(default=None, primary_key=True)
        city: str

    class D5Shipment(Model):
        id: int | None = Field(default=None, primary_key=True)
        warehouse: Annotated[D5Warehouse, ForeignKey(related_name="shipments")]

    wh = D5Warehouse(code=7, city="Reno")
    shipment = D5Shipment(warehouse=wh)
    assert shipment.warehouse_id == 7


def test_fk_extraction_with_target_pk_named_id_still_works():
    class D5Author(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class D5Book(Model):
        id: int | None = Field(default=None, primary_key=True)
        author: Annotated[D5Author, ForeignKey(related_name="books")]

    a = D5Author(id=3, name="x")
    b = D5Book(author=a)
    assert b.author_id == 3
