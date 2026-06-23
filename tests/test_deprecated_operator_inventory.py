import inspect
import re
from pathlib import Path

import pytest

from tests import test_query_builder, test_query_typing


def _marker_names(obj: object) -> set[str]:
    marks = getattr(obj, "pytestmark", [])
    if not isinstance(marks, list):
        marks = [marks]
    return {mark.name for mark in marks if hasattr(mark, "name")}


def test_deprecated_operator_inventory_is_tracked():
    expected_builder_tests = {
        "test_model_where_clause",
        "test_query_chaining_placeholders",
    }
    expected_typing_classes = {
        "TestOperatorPathUnchanged",
        "TestCombinedStyles",
    }

    marked_builder_tests = {
        name
        for name, value in vars(test_query_builder).items()
        if name.startswith("test_")
        and callable(value)
        and "deprecated_operator_path" in _marker_names(value)
    }
    assert marked_builder_tests == expected_builder_tests

    marked_typing_classes = {
        name
        for name, value in vars(test_query_typing).items()
        if inspect.isclass(value) and "deprecated_operator_path" in _marker_names(value)
    }
    assert marked_typing_classes == expected_typing_classes


def test_pytest_marker_documents_v013_removal_target():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    marker_entry = re.search(
        r'deprecated_operator_path:[^"]*v0\.14\.0[^"]*',
        content,
    )
    assert marker_entry is not None
