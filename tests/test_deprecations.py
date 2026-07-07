import pytest

from ferro._deprecations import (
    deprecated,
    deprecation_message,
    warn_deprecated,
)


def test_deprecation_message_includes_since_and_remove_in():
    message = deprecation_message(
        reason="Example API is deprecated.",
        since="v1.0.0",
        remove_in="v2.0.0",
    )
    assert "Example API is deprecated." in message
    assert "Deprecated since v1.0.0." in message
    assert "Planned removal in v2.0.0." in message


def test_deprecation_message_includes_reference():
    message = deprecation_message(
        reason="Example API is deprecated.",
        since="v1.0.0",
        remove_in="v2.0.0",
        reference="https://example.com/migration",
    )
    assert "See https://example.com/migration." in message


def test_deprecated_decorator_emits_warning():
    @deprecated(
        reason="Legacy helper is deprecated.",
        since="v1.0.0",
        remove_in="v2.0.0",
    )
    def legacy_helper() -> str:
        return "ok"

    with pytest.deprecated_call(
        match=r"Legacy helper is deprecated\..*v1\.0\.0.*v2\.0\.0"
    ):
        assert legacy_helper() == "ok"


def test_warn_deprecated_emits_warning():
    with pytest.deprecated_call(match="Inline legacy path is deprecated.*v2\\.0\\.0"):
        warn_deprecated(
            reason="Inline legacy path is deprecated.",
            since="v1.0.0",
            remove_in="v2.0.0",
        )
