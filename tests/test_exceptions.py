"""Contract tests for the ferro.exceptions hierarchy (FF-A / A2, #172)."""

import pickle

import pytest

import ferro
from ferro.exceptions import (
    CheckViolationError,
    DataError,
    FerroError,
    ForeignKeyViolationError,
    IntegrityError,
    InterfaceError,
    ModelDoesNotExist,
    NotNullViolationError,
    OperationalError,
    UniqueViolationError,
)


class TestHierarchy:
    def test_ferro_error_is_the_root(self):
        for exc in (
            InterfaceError,
            OperationalError,
            DataError,
            IntegrityError,
            ModelDoesNotExist,
        ):
            assert issubclass(exc, FerroError)

    def test_violations_are_integrity_errors(self):
        for exc in (
            UniqueViolationError,
            ForeignKeyViolationError,
            NotNullViolationError,
            CheckViolationError,
        ):
            assert issubclass(exc, IntegrityError)
            assert issubclass(exc, FerroError)

    def test_branches_are_disjoint(self):
        assert not issubclass(IntegrityError, OperationalError)
        assert not issubclass(OperationalError, IntegrityError)
        assert not issubclass(DataError, IntegrityError)
        assert not issubclass(InterfaceError, OperationalError)

    def test_model_does_not_exist_remains_lookup_error(self):
        """Pre-FF-A callers catching LookupError must keep working."""
        assert issubclass(ModelDoesNotExist, LookupError)

        class Dummy:
            pass

        with pytest.raises(LookupError):
            raise ModelDoesNotExist(Dummy, 1)
        with pytest.raises(FerroError):
            raise ModelDoesNotExist(Dummy, 1)


class TestStructuredAttributes:
    def test_defaults_are_none(self):
        exc = OperationalError("connect failed")
        assert exc.driver_message is None
        assert exc.sqlstate is None
        assert exc.constraint is None
        assert str(exc) == "connect failed"

    def test_attributes_are_kept(self):
        exc = UniqueViolationError(
            "Save failed: duplicate key",
            driver_message="duplicate key value violates unique constraint",
            sqlstate="23505",
            constraint="uq_user_email",
        )
        assert exc.driver_message == (
            "duplicate key value violates unique constraint"
        )
        assert exc.sqlstate == "23505"
        assert exc.constraint == "uq_user_email"
        assert str(exc) == "Save failed: duplicate key"

    def test_pickle_round_trip(self):
        exc = UniqueViolationError(
            "boom", driver_message="dup", sqlstate="23505", constraint="uq_x"
        )
        clone = pickle.loads(pickle.dumps(exc))
        assert type(clone) is UniqueViolationError
        assert str(clone) == "boom"
        assert clone.driver_message == "dup"
        assert clone.sqlstate == "23505"
        assert clone.constraint == "uq_x"

    def test_model_does_not_exist_keeps_bespoke_init(self):
        class Dummy:
            pass

        exc = ModelDoesNotExist(Dummy, 42)
        assert exc.model is Dummy
        assert exc.pk == 42
        assert "Dummy" in str(exc)
        assert "42" in str(exc)


class TestPublicSurface:
    def test_exported_from_ferro(self):
        for name in (
            "FerroError",
            "InterfaceError",
            "OperationalError",
            "DataError",
            "IntegrityError",
            "UniqueViolationError",
            "ForeignKeyViolationError",
            "NotNullViolationError",
            "CheckViolationError",
            "ModelDoesNotExist",
        ):
            assert hasattr(ferro, name), f"ferro.{name} missing"
            assert name in ferro.__all__

    def test_module_is_ferro_exceptions(self):
        """Classes must not leak a private module path into tracebacks."""
        assert UniqueViolationError.__module__ == "ferro.exceptions"
        assert FerroError.__module__ == "ferro.exceptions"
