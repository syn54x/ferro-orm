from decimal import Decimal
from enum import Enum
from typing import Annotated

from ferro._annotation_utils import (
    annotation_is_decimal,
    enum_subclass_from_annotation,
)


class Color(Enum):
    RED = "red"


def test_enum_subclass_through_optional_and_annotated():
    assert enum_subclass_from_annotation(Color | None) is Color
    assert enum_subclass_from_annotation(Annotated[Color, "x"]) is Color
    assert enum_subclass_from_annotation(int) is None


def test_annotation_is_decimal_through_optional():
    assert annotation_is_decimal(Decimal | None) is True
    assert annotation_is_decimal(Annotated[Decimal, "x"]) is True
    assert annotation_is_decimal(float) is False
