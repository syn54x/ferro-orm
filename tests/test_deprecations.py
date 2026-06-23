import warnings

import pytest

from ferro._deprecations import (
    IR_FIRST_DEPRECATION_REMOVE_IN,
    IR_FIRST_DEPRECATION_SINCE,
    deprecated,
    deprecation_message,
    enable_deprecation_warnings,
    warn_deprecated,
)


def test_deprecation_message_includes_since_and_remove_in():
    message = deprecation_message(
        reason="Example API is deprecated.",
        since=IR_FIRST_DEPRECATION_SINCE,
        remove_in=IR_FIRST_DEPRECATION_REMOVE_IN,
    )
    assert "Example API is deprecated." in message
    assert f"Deprecated since {IR_FIRST_DEPRECATION_SINCE}." in message
    assert f"Planned removal in {IR_FIRST_DEPRECATION_REMOVE_IN}." in message


def test_deprecation_message_includes_reference_link():
    message = deprecation_message(
        reason="Example API is deprecated.",
        since=IR_FIRST_DEPRECATION_SINCE,
        remove_in=IR_FIRST_DEPRECATION_REMOVE_IN,
        reference="https://example.com/migrate",
    )
    assert message.endswith("See https://example.com/migrate.")


def test_deprecated_decorator_emits_warning():
    @deprecated(
        reason="Legacy helper is deprecated.",
        since=IR_FIRST_DEPRECATION_SINCE,
        remove_in=IR_FIRST_DEPRECATION_REMOVE_IN,
    )
    def legacy_helper() -> str:
        return "ok"

    with pytest.deprecated_call(
        match=r"Legacy helper is deprecated\..*v0\.12\.0.*v0\.14\.0"
    ):
        assert legacy_helper() == "ok"


def test_warn_deprecated_emits_warning():
    with pytest.deprecated_call(match="Inline legacy path is deprecated.*v0\\.14\\.0"):
        warn_deprecated(
            reason="Inline legacy path is deprecated.",
            since=IR_FIRST_DEPRECATION_SINCE,
            remove_in=IR_FIRST_DEPRECATION_REMOVE_IN,
        )


def test_enable_deprecation_warnings_surfaces_library_deprecations():
    with warnings.catch_warnings(record=True) as captured:
        warnings.resetwarnings()
        enable_deprecation_warnings()

        def _library_emitter() -> None:
            warnings.warn("library deprecation", DeprecationWarning, stacklevel=1)

        _library_emitter()

    assert any(
        issubclass(item.category, DeprecationWarning)
        and str(item.message) == "library deprecation"
        for item in captured
    )
