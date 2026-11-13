"""ColumnSpec builder: one derivation site per column fact (grilling 2026-07-08)."""

from typing import Annotated
from uuid import UUID

from ferro import Field, Model
from ferro.base import FerroField
from ferro.columns import build_column_specs, fk_column_spec


class TestAutoincrementDerivation:
    """Pins the unified autoincrement rule (ADR-0002): explicit, else pk AND integer-typed."""

    def test_ferro_path_integer_pk_defaults_true(self, clean_registry):
        class M(Model):
            id: Annotated[int, FerroField(primary_key=True)]

        assert build_column_specs(M)["id"].autoincrement is True

    def test_ferro_path_str_pk_defaults_false(self, clean_registry):
        class M(Model):
            id: Annotated[str, FerroField(primary_key=True)]

        assert build_column_specs(M)["id"].autoincrement is False

    def test_field_integer_pk_defaults_true(self, clean_registry):
        class M(Model):
            id: int = Field(default=None, primary_key=True)

        assert build_column_specs(M)["id"].autoincrement is True

    def test_field_str_pk_defaults_false_unified(self, clean_registry):
        # ADR-0002: one rule for both spellings - explicit, else (pk AND integer).
        class M(Model):
            id: str = Field(default=None, primary_key=True)

        assert build_column_specs(M)["id"].autoincrement is False

    def test_explicit_always_wins(self, clean_registry):
        class M(Model):
            id: Annotated[int, FerroField(primary_key=True, autoincrement=False)]

        assert build_column_specs(M)["id"].autoincrement is False


class TestNullableDerivation:
    def test_pk_clamped_not_null_despite_optional_annotation(self, clean_registry):
        class M(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        spec = build_column_specs(M)["id"]
        assert spec.nullable is False and spec.python_type == (int | None)

    def test_explicit_nullable_overrides_annotation(self, clean_registry):
        class M(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            note: Annotated[str | None, FerroField(nullable=False)] = None

        assert build_column_specs(M)["note"].nullable is False

    def test_infer_from_annotation(self, clean_registry):
        class M(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            a: str
            b: str | None = None

        specs = build_column_specs(M)
        assert specs["a"].nullable is False and specs["b"].nullable is True


class TestTypeFacts:
    def test_decimal_format_and_logical_type(self, clean_registry):
        from decimal import Decimal

        class M(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            price: Decimal

        spec = build_column_specs(M)["price"]
        assert spec.format == "decimal" and spec.logical_type == "decimal"

    def test_db_type_explicit(self, clean_registry):
        class M(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            title: Annotated[str, FerroField(db_type="varchar(255)")]

        spec = build_column_specs(M)["title"]
        assert spec.db_type == "varchar(255)" and spec.db_type_explicit is True


class TestJsonFamilyDefaultSnapshot:
    """#373: json-family factories snapshot onto ColumnSpec.default; scalar factories do not."""

    def test_dict_factory_snapshots_empty_object(self, clean_registry):
        from ferro import Field

        class M(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            turns: dict[str, dict] = Field(default_factory=dict)

        assert build_column_specs(M)["turns"].default == {}

    def test_list_factory_snapshots_empty_array(self, clean_registry):
        from ferro import Field

        class M(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            tags: list[str] = Field(default_factory=list)

        assert build_column_specs(M)["tags"].default == []

    def test_static_object_default_stays_on_spec(self, clean_registry):
        from ferro import Field

        class M(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            flags: dict[str, int] = Field(default={"role": "guest"})

        assert build_column_specs(M)["flags"].default == {"role": "guest"}

    def test_uuid_factory_is_not_snapshotted(self, clean_registry):
        from uuid import uuid4

        from ferro import Field

        class M(Model):
            id: UUID = Field(default_factory=uuid4, primary_key=True)

        assert build_column_specs(M)["id"].default is None

    def test_datetime_factory_is_not_snapshotted(self, clean_registry):
        from datetime import datetime

        from ferro import Field

        class M(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            created_at: datetime = Field(default_factory=datetime.now)

        assert build_column_specs(M)["created_at"].default is None

    def test_raising_json_factory_leaves_default_unset(self, clean_registry):
        from ferro import Field

        def boom() -> dict:
            raise RuntimeError("nope")

        class M(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            payload: dict = Field(default_factory=boom)

        assert build_column_specs(M)["payload"].default is None

    def test_annotated_dict_factory_snapshots(self, clean_registry):
        from ferro import Field

        class M(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            turns: Annotated[dict[str, dict], Field(default_factory=dict)]

        assert build_column_specs(M)["turns"].default == {}

    def test_nested_model_factory_snapshots_object(self, clean_registry):
        from pydantic import BaseModel as PydanticModel

        from ferro import Field

        class Settings(PydanticModel):
            theme: str = "dark"

        class M(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            settings: Settings = Field(default_factory=Settings)

        assert build_column_specs(M)["settings"].default == {"theme": "dark"}


class TestJoinTableProducers:
    def test_fk_column_spec_takes_the_key_s_type_and_storage(self, clean_registry):
        class User(Model):
            id: UUID | None = Field(default=None, primary_key=True)
            code: str = Field(unique=True, index=True, db_type="varchar(36)")

        key = build_column_specs(User)["code"]
        spec = fk_column_spec("user_code", key, to_table="user")

        assert (spec.logical_type, spec.python_type) == ("string", str)
        assert (spec.db_type, spec.db_type_explicit) == ("varchar(36)", True)
        assert not (spec.unique or spec.index or spec.nullable)

    def test_fk_column_spec_strips_none_and_drops_the_key_s_own_facts(
        self, clean_registry
    ):
        class User(Model):
            id: UUID | None = Field(default=None, primary_key=True)

        spec = fk_column_spec(
            "user_id", build_column_specs(User)["id"], to_table="user"
        )

        assert (spec.logical_type, spec.python_type) == ("uuid", UUID)
        assert spec.primary_key is False and spec.default is None
        assert spec.foreign_key.to_table == "user"
        assert spec.foreign_key.on_delete == "CASCADE"
