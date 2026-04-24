"""Schema registration for Enum fields when annotations are deferred (PEP 563/649)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from ferro import FerroField, Model


class FileFormat(StrEnum):
    """Like ``TranscriptFormat`` in app code — string-valued enum."""

    PDF = "pdf"
    JSON = "json"


class ModelWithStrEnumField(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    format: FileFormat


def test_enum_type_name_registered_for_future_annotated_strenum():
    """Deferred string annotations must still yield ``enum_type_name`` in ``__ferro_schema__``."""
    props = ModelWithStrEnumField.__ferro_schema__["properties"]
    assert props["format"].get("enum_type_name") == "fileformat"
