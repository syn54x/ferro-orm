"""The metaclass that turns a model class declaration into a registered table."""

import re
import sys
from dataclasses import replace
from functools import partial
from enum import Enum
from typing import (
    Annotated,
    Any,
    Callable,
    ClassVar,
    ForwardRef,
    Optional,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import AfterValidator, BaseModel, SecretBytes, SecretStr, ValidationError
from pydantic import Field as PydanticField
from pydantic.fields import FieldInfo
from pydantic_core import PydanticCustomError, PydanticUndefined

from ._annotation_eval import evaluate_foreign_key_annotation
from ._annotation_utils import (
    _strip_optional_union as _annotation_utils_strip_optional_union,
)
from ._annotation_utils import annotation_allows_none, is_closed_domain_annotation
from ._shadow_fk_types import shadow_annotation_for_foreign_key
from .base import FerroField, ForeignKey, GeneratedExpression, ManyToManyRelation
from .columns import build_column_specs, refuse_back_ref_over_retired_relation
from .fields import FERRO_FIELD_EXTRA_KEY
from .ir import compile_model_schema_ir
from .ir.compiler import declared_retired_labels
from .query import Relation
from .registry import REGISTRY
from .relations.descriptors import ForwardDescriptor

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")


def _assert_weakref_support(cls: type) -> None:
    """Reject model classes whose instances cannot be weakly referenced.

    The identity map holds weak references to instances (FF-D D1). A class
    that suppresses ``__weakref__`` would force a silent strong-ref fallback,
    which is exactly the unbounded-memory failure F3 removed — so it fails
    loudly at class definition time instead.
    """
    if not any("__weakref__" in getattr(base, "__dict__", {}) for base in cls.__mro__):
        raise TypeError(
            f"{cls.__name__} instances do not support weak references "
            "(no __weakref__ slot in the MRO). Ferro model instances must be "
            "weakly referenceable for identity mapping; remove __slots__ "
            "declarations that suppress __weakref__."
        )


def _type_name(annotation: Any) -> str:
    """``str`` for ``str``, ``Team`` for ``Team`` or ``"Team"``."""
    if isinstance(annotation, ForwardRef):
        return annotation.__forward_arg__
    if isinstance(annotation, str):
        return annotation
    return getattr(annotation, "__name__", None) or repr(annotation)


def _retired_not_optional(model: str, field_name: str, annotation: Any) -> TypeError:
    return TypeError(
        f"{model}.{field_name} is retired but not optional: declare it "
        f"`{_type_name(annotation)} | None`"
    )


def _retired_not_nullable(model: str, field_name: str) -> TypeError:
    return TypeError(
        f"{model}.{field_name} is retired but declared `nullable=False`: a retired "
        "column stays nullable; drop `nullable=False`"
    )


def _retired_primary_key(model: str, field_name: str) -> TypeError:
    return TypeError(
        f"{model}.{field_name}: a primary key cannot be retired; drop "
        "`retired=True`. To remove the key, change the primary key first: see "
        "the Migrations docs § Changing a primary key"
    )


def _retired_and_renamed(model: str, field_name: str) -> TypeError:
    return TypeError(
        f"{model}.{field_name}: retire it or rename it, not both; drop "
        "`renamed_from` once its migration is generated"
    )


def _reverse_relation_retired(model: str, field_name: str, m2m: bool) -> TypeError:
    if m2m:
        return TypeError(
            f"{model}.{field_name}: a ManyToMany cannot be retired; it is a join "
            "table, not a column. Delete the declaration to drop the join table"
        )
    return TypeError(
        f"{model}.{field_name}: a BackRef cannot be retired; it is not a column. "
        "Delete the BackRef"
    )


class SecretHydrator:
    """A hydration converter that wraps a decoded value in its secret type.

    The database stores what the secret hides, so a ``SecretStr`` column hands
    back ``str`` and a ``SecretBytes`` column ``bytes``; any other value is
    corrupt data and is refused. The refusal names the types only, never the
    value, which may be the secret itself.
    """

    __slots__ = ("secret_class", "_stored_type")

    def __init__(self, secret_class: type[SecretStr] | type[SecretBytes]) -> None:
        self.secret_class = secret_class
        self._stored_type = bytes if issubclass(secret_class, SecretBytes) else str

    def __call__(self, value: Any) -> SecretStr | SecretBytes:
        if type(value) is not self._stored_type:
            raise TypeError(
                f"{self.secret_class.__name__} stores {self._stored_type.__name__}, "
                f"but the column holds {type(value).__name__}"
            )
        return self.secret_class(value)

    def __repr__(self) -> str:
        return f"SecretHydrator({self.secret_class.__name__})"


def _written_retired_label(value: Any, labels: frozenset[str]) -> str | None:
    """The retired label ``value`` would write, or ``None``.

    Compared by value: a ``StrEnum`` member, a plain ``Enum`` member and the
    raw label string all write the member's value, so each is caught.
    """
    raw = value.value if isinstance(value, Enum) else value
    if isinstance(raw, str) and raw in labels:
        return raw
    return None


def refuse_retired_label(
    enum_cls: type[Enum], labels: frozenset[str], value: Any
) -> Any:
    """Refuse a value that would write one of an enum's retired labels.

    Every path that writes an enum column refuses a retired label the same
    way: Pydantic validation calls this function, and
    [`refuse_retired_label_write`][ferro.metaclass.refuse_retired_label_write]
    calls it for the writes that skip validation. Each surfaces as a
    `ValidationError` of type `retired_enum_label`. See
    [Retiring an enum label](../guide/schema/renames-and-retirements.md#retiring-an-enum-label).

    Args:
        enum_cls: The column's enum class.
        labels: The enum's retired labels.
        value: The value being written: a member or a raw label.

    Returns:
        `value`, unchanged, when it writes no retired label.

    Raises:
        PydanticCustomError: The value writes a retired label; the error
            names the member.
    """
    label = _written_retired_label(value, labels)
    if label is None:
        return value
    member = next(m for m in enum_cls if m.value == label)
    raise PydanticCustomError(
        "retired_enum_label",
        f"{enum_cls.__name__}.{member.name} ('{label}') is retired in this release: "
        "the previous release may still write it, this one does not",
    )


def refuse_retired_label_write(model_cls: type, column: str, value: Any) -> None:
    """Refuse a retired enum label written without Pydantic validation.

    Assigning an attribute (without `validate_assignment`) and `update()`
    write a column without validating it, so they check here and raise the
    `ValidationError` that validation would have raised.

    Args:
        model_cls: The model class being written.
        column: The column being written.
        value: The value being written.

    Raises:
        ValidationError: The value writes a retired label of the column's
            enum, with error type `retired_enum_label` at `(column,)`.
    """
    enum_cls = getattr(model_cls, "_enum_fields", {}).get(column)
    if enum_cls is None:
        return
    try:
        refuse_retired_label(enum_cls, declared_retired_labels(enum_cls), value)
    except PydanticCustomError as refusal:
        raise ValidationError.from_exception_data(
            model_cls.__name__,
            [{"type": refusal, "loc": (column,), "input": value}],
        ) from None


def _guarded_setattr(guarded: frozenset[str], inherited: Any):
    def __setattr__(self: BaseModel, name: str, value: Any) -> None:
        if name in guarded:
            refuse_retired_label_write(type(self), name, value)
        inherited(self, name, value)

    return __setattr__


def generated_refusal(model_cls: type, name: str) -> str:
    """The sentence a write to a generated column is refused with."""
    inputs = ", ".join(sorted(model_cls.__ferro_generated__[name]))
    return (
        f"{model_cls.__name__}.{name} is a generated column: the database "
        f"computes it from {inputs} on every write"
    )


def _generated_setattr(inherited: Any):
    """Refuse assigning a generated field; clear it when an input is assigned.

    The instance holds the value the database returned. Once a column the
    expression reads changes, that value is stale, so the field reads `None`
    until the next save reads it back (ADR-0058).
    """

    def __setattr__(self: BaseModel, name: str, value: Any) -> None:
        generated = type(self).__ferro_generated__
        if name in generated:
            raise TypeError(
                f"{generated_refusal(type(self), name)}; assign those columns "
                "and save() instead"
            )
        inherited(self, name, value)
        for field_name, inputs in generated.items():
            if name in inputs:
                self.__dict__[field_name] = None

    return __setattr__


class ModelMetaclass(type(BaseModel)):
    """The metaclass of [`Model`][ferro.Model], which records each model's schema when the class is defined.

    Defining a model class checks its declaration and records its table in
    the model registry. The database learns about it on the next
    `connect()`, `create_tables()` or `migrate()`.
    """

    def __new__(mcs, name, bases, namespace, **kwargs):
        # Phase 1: Annotation Processing
        annotations = mcs._resolve_deferred_annotations(namespace)
        namespace["__annotations__"] = annotations

        # The scope that runs the class statement: a model defined inside a
        # function names its sibling targets there, not in the module.
        caller = sys._getframe(1)
        frame_ns = (
            dict(caller.f_locals)
            if caller.f_globals.get("__name__") == namespace.get("__module__")
            and caller.f_locals is not caller.f_globals
            else None
        )
        local_relations, fields_to_remove, pending_relations, retired_relations = (
            mcs._scan_relationship_annotations(
                annotations, namespace, name, frame_ns=frame_ns
            )
        )
        mcs._inject_shadow_fields(annotations, namespace, local_relations)
        mcs._prepare_namespace_for_pydantic(namespace, annotations, fields_to_remove)

        # Phase 2: Class Creation
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)

        # Verify weakref support for identity mapping (FF-D D1)
        _assert_weakref_support(cls)

        # Phase 3: Post-Creation Setup
        if name == "Model":
            return cls

        cls.__ferro_identity__ = f"{cls.__module__}.{cls.__qualname__}"
        cls.__ferro_table__ = mcs._resolve_table_name(name, namespace)
        # Private seam for IR-derived classes (``migrations.historical``): read
        # from the class's own namespace only, never inherited, always set so
        # ``build_column_specs`` can tell a user model from a historical one.
        setattr(
            cls, "__ferro_historical__", namespace.get("__ferro_historical__") is True
        )
        cls.__ferro_renamed_from__ = mcs._resolve_renamed_from(name, namespace)

        # Refusals raise from the schema compile, which itself writes the
        # registry (the envelope, join tables), so no order puts every refusal
        # before every write: a refused class is rolled back out of every
        # store instead, leaving the registry as it was before the class body.
        before = REGISTRY.snapshot()
        try:
            for field_name, metadata in pending_relations:
                REGISTRY.defer_relation(cls.__ferro_identity__, field_name, metadata)

            mcs._register_model(cls, cls.__ferro_identity__, local_relations)
            ferro_fields = mcs._parse_ferro_field_metadata(cls)
            cls.ferro_fields = ferro_fields
            mcs._validate_db_type_options(cls, ferro_fields)
            mcs._validate_generated_options(cls, ferro_fields)
            mcs._retire_fields(cls, retired_relations)
            mcs._inject_relation_descriptors(cls, local_relations)
            mcs._generate_and_register_schema(cls, cls.__ferro_identity__)

            # Spec-derived runtime attributes (read specs at use time everywhere
            # else; these two bake *names only*, which are stable across epochs).
            cls.__ferro_query_columns__ = frozenset(cls.__ferro_columns__)
            mcs._register_enum_fields(cls)
            mcs._register_hydration_converters(cls)
        except BaseException:
            REGISTRY.restore(before)
            raise
        if cls.__ferro_generated__:
            cls.__setattr__ = _generated_setattr(cls.__setattr__)

        return cls

    @staticmethod
    def _retire_fields(cls, retired_relations: dict[str, ForeignKey]) -> None:
        """Take the retired fields out of the model (ADR-0053).

        A retired scalar field's column spec is derived here, once, while the
        field is still a Pydantic field (its annotation resolved, its ``db_type``
        and enum read the one way every column's are); then the field leaves
        the Pydantic model, so the model has no attribute, keyword or dump key
        for it. A retired relation never became a field: the class-body scan
        kept it out of ``ferro_relations`` and gave it no shadow column.

        Raises:
            TypeError: a retired primary key, a retired field that also
                declares ``renamed_from`` or ``nullable=False``, or one whose
                annotation does not allow ``None`` (each naming the fix).
        """
        model = cls.__name__
        retired = {
            field_name: meta
            for field_name, meta in cls.ferro_fields.items()
            if getattr(meta, "retired", False)
        }
        for field_name, meta in retired.items():
            if meta.primary_key:
                raise _retired_primary_key(model, field_name)
            if meta.renamed_from is not None:
                raise _retired_and_renamed(model, field_name)
            if meta.nullable is False:
                raise _retired_not_nullable(model, field_name)
            annotation = cls.model_fields[field_name].annotation
            if not annotation_allows_none(annotation):
                raise _retired_not_optional(model, field_name, annotation)

        retired_fields = {}
        if retired:
            specs = build_column_specs(cls)
            retired_fields = {
                field_name: replace(specs[field_name], nullable=True, retired=True)
                for field_name in retired
            }
            for field_name in retired:
                del cls.__pydantic_fields__[field_name]
                del cls.ferro_fields[field_name]
            annotations = dict(cls.__dict__.get("__annotations__", {}))
            for field_name in retired:
                annotations.pop(field_name, None)
            cls.__annotations__ = annotations
            cls.model_rebuild(force=True, raise_errors=False)

        for field_name, meta in retired_relations.items():
            refuse_back_ref_over_retired_relation(cls, field_name, meta)
        cls.__ferro_retired_fields__ = retired_fields
        cls.__ferro_retired_relations__ = retired_relations
        cls.__ferro_retired_names__ = frozenset(
            [
                *retired_fields,
                *retired_relations,
                *(f"{field_name}_id" for field_name in retired_relations),
            ]
        )

    @staticmethod
    def _resolve_table_name(name: str, namespace: dict) -> str:
        """Resolve the physical table name for a model class (FF-E E2).

        ``__ferro_table__`` is honored only when declared in the class's own
        body (the metaclass namespace) — a subclass never silently inherits
        its parent's physical table. Default: ``classname.lower()``.
        """
        configured = namespace.get("__ferro_table__")
        if configured is None:
            return name.lower()
        if not isinstance(configured, str):
            raise TypeError(
                f"__ferro_table__ on model '{name}' must be a str, "
                f"got {type(configured).__name__}"
            )
        if len(configured) > 63 or not _TABLE_NAME_RE.match(configured):
            raise ValueError(
                f"__ferro_table__ {configured!r} on model '{name}' is not a "
                "valid table name: expected a 1-63 character identifier "
                "matching [A-Za-z_][A-Za-z0-9_]*"
            )
        return configured

    @staticmethod
    def _resolve_renamed_from(name: str, namespace: dict) -> str | None:
        """Resolve a model's table rename hint (ADR-0032).

        ``__ferro_renamed_from__`` names the table this model's table was
        called before. Like ``__ferro_table__`` it is honored only in the
        class's own body: a subclass never inherits its parent's rename.
        """
        declared = namespace.get("__ferro_renamed_from__")
        if declared is None:
            return None
        if not isinstance(declared, str):
            raise TypeError(
                f"__ferro_renamed_from__ on model '{name}' must be a str, "
                f"got {type(declared).__name__}"
            )
        if len(declared) > 63 or not _TABLE_NAME_RE.match(declared):
            raise ValueError(
                f"__ferro_renamed_from__ {declared!r} on model '{name}' is not a "
                "valid table name: expected a 1-63 character identifier "
                "matching [A-Za-z_][A-Za-z0-9_]*"
            )
        return declared

    @staticmethod
    def _field_ferro_payload(obj: Any) -> dict[str, Any]:
        """Return Ferro metadata payload from a wrapped FieldInfo."""
        if not isinstance(obj, FieldInfo):
            return {}
        extra = getattr(obj, "json_schema_extra", None)
        if not isinstance(extra, dict):
            return {}
        payload = extra.get(FERRO_FIELD_EXTRA_KEY, {})
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _strip_optional_union(hint: Any) -> Any:
        """Unwrap ``T | None`` / ``Optional[T]`` to ``T`` for relationship detection."""
        return _annotation_utils_strip_optional_union(hint)

    @staticmethod
    def _relationship_marker_from_annotation(hint: Any) -> Any:
        """Inner type used to inspect relationship annotations."""
        if get_origin(hint) is Annotated:
            args = get_args(hint)
            if args:
                return ModelMetaclass._strip_optional_union(args[0])
            return hint
        return ModelMetaclass._strip_optional_union(hint)

    @staticmethod
    def _legacy_back_ref_error(field_name: str) -> TypeError:
        return TypeError(
            f"Field '{field_name}' uses deprecated BackRef[...] annotation syntax. "
            "Use Relation[list[T]] = BackRef() for collection back-references."
        )

    @staticmethod
    def _relationship_field_payload(
        field_name: str, hint: Any, namespace: dict
    ) -> dict[str, Any]:
        """Return relationship metadata supplied by ferro.Field helpers."""
        origin = get_origin(hint)
        default_val = namespace.get(field_name)
        payload = ModelMetaclass._field_ferro_payload(default_val)
        if payload:
            return payload

        if origin is Annotated:
            for metadata in get_args(hint)[1:]:
                payload = ModelMetaclass._field_ferro_payload(metadata)
                if payload:
                    return payload

        return {}

    @staticmethod
    def _relation_target_from_annotation(field_name: str, hint: Any) -> Any:
        """Extract T from Relation[list[T]] for collection relationships."""
        marker = ModelMetaclass._relationship_marker_from_annotation(hint)
        if isinstance(marker, str):
            return ModelMetaclass._relation_target_from_string(field_name, marker)
        if get_origin(marker) is not Relation:
            raise TypeError(
                f"Field '{field_name}' must be annotated as Relation[list[T]] "
                "when using BackRef(), ManyToMany(), or relationship Field flags."
            )

        args = get_args(marker)
        if not args:
            raise TypeError(f"Field '{field_name}' must specify Relation[list[T]].")

        relation_arg = ModelMetaclass._strip_optional_union(args[0])
        if get_origin(relation_arg) is not list:
            raise TypeError(
                f"Field '{field_name}' must use Relation[list[T]] for collection relationships."
            )

        inner_args = get_args(relation_arg)
        if not inner_args:
            raise TypeError(f"Field '{field_name}' must specify Relation[list[T]].")
        return ModelMetaclass._strip_optional_union(inner_args[0])

    @staticmethod
    def _relation_target_from_string(field_name: str, hint: str) -> str:
        """Extract T from a string ``Relation[list[T]]`` annotation."""
        normalized = hint.replace(" ", "")
        prefix = "Relation[list["
        if not normalized.startswith(prefix) or not normalized.endswith("]]"):
            raise TypeError(
                f"Field '{field_name}' must be annotated as Relation[list[T]] "
                "when using BackRef(), ManyToMany(), or relationship Field flags."
            )
        target = normalized[len(prefix) : -2]
        return target.strip("\"'")

    @staticmethod
    def _annotation_is_plain_list(hint: Any) -> bool:
        marker = ModelMetaclass._relationship_marker_from_annotation(hint)
        if get_origin(marker) is list:
            return True
        return isinstance(marker, str) and marker.replace(" ", "").startswith("list[")

    @staticmethod
    def _annotation_looks_like_back_ref(hint: Any) -> bool:
        if isinstance(hint, str) and "BackRef" in hint:
            return True
        if isinstance(hint, ForwardRef) and "BackRef" in hint.__forward_arg__:
            return True
        return False

    @staticmethod
    def _resolve_deferred_annotations(namespace: dict) -> dict[str, Any]:
        """
        Resolve deferred annotations (PEP 649) if present.

        Returns:
            Dictionary of resolved annotations
        """
        # Handle Python 3.14+ deferred annotations
        # We need a complete __annotations__ dict so we can safely modify it.
        if "__annotate_func__" in namespace and "__annotations__" not in namespace:
            try:
                # Format 1: Value (evaluated)
                return namespace["__annotate_func__"](1)
            except Exception as value_error:
                if "BackRef[...]" in str(value_error):
                    raise value_error
                try:
                    # Format 2: ForwardRef (non-evaluated objects)
                    return namespace["__annotate_func__"](2)
                except Exception as forward_error:
                    if "BackRef[...]" in str(forward_error):
                        raise forward_error
                    # #155: was `pass` -> returned {} -> Pydantic then raised the
                    # misleading "non-annotated attribute" on the first class-body
                    # assignment. Surface the REAL cause (PEP 649 deferred-eval
                    # failure) instead of silently dropping every annotation.
                    model = (
                        namespace.get("__qualname__")
                        or namespace.get("__name__")
                        or "<model>"
                    )
                    value_error.add_note(
                        f"ferro: could not resolve deferred annotations for model "
                        f"'{model}' (PEP 649 / Python 3.14). An annotation on this "
                        f"model failed to evaluate; an attribute Pydantic may flag "
                        f"as 'non-annotated' is a symptom, not the cause."
                    )
                    raise value_error

        return namespace.get("__annotations__", {})

    @staticmethod
    def _scan_relationship_annotations(
        annotations: dict,
        namespace: dict,
        model_name: str,
        frame_ns: dict | None = None,
    ) -> tuple[dict, list, list, dict]:
        """
        Scan annotations for relationship fields (BackRef, ForeignKey, ManyToMany).

        Returns:
            (local_relations, fields_to_remove, pending_relations,
            retired_relations): Relationship metadata, fields to hide from
            Pydantic, ``(field_name, metadata)`` entries deferred to the
            Registry's pending-relation queue (flushed under the model's
            resolved ``__ferro_identity__`` after the class object exists), and
            the retired ``ForeignKey`` relations (ADR-0053), which leave the
            class body entirely.
        """
        local_relations = {}
        fields_to_remove = []
        pending_relations = []
        retired_relations: dict[str, ForeignKey] = {}

        for field_name, hint in list(annotations.items()):
            if ModelMetaclass._annotation_looks_like_back_ref(hint):
                raise ModelMetaclass._legacy_back_ref_error(field_name)

            relationship_payload = ModelMetaclass._relationship_field_payload(
                field_name, hint, namespace
            )
            is_back_field = relationship_payload.get("back_ref") is True
            is_m2m_field = relationship_payload.get("many_to_many") is True
            if relationship_payload.get("retired") is True and (
                is_back_field or is_m2m_field
            ):
                raise _reverse_relation_retired(model_name, field_name, is_m2m_field)

            if is_back_field and is_m2m_field:
                raise TypeError(
                    f"Field '{field_name}' cannot be both back_ref and many_to_many."
                )

            if is_back_field:
                marker = ModelMetaclass._relationship_marker_from_annotation(hint)
                if get_origin(marker) is Relation:
                    ModelMetaclass._relation_target_from_annotation(field_name, hint)
                elif ModelMetaclass._annotation_is_plain_list(hint):
                    raise TypeError(
                        f"Field '{field_name}' uses a plain list annotation. Use "
                        "Relation[list[T]] = BackRef() for collection back-references."
                    )
                local_relations[field_name] = "BackRef"
                fields_to_remove.append(field_name)
                continue

            if is_m2m_field:
                target = ModelMetaclass._relation_target_from_annotation(
                    field_name, hint
                )
                related_name = relationship_payload.get("related_name")
                if not related_name:
                    raise TypeError(
                        f"Field '{field_name}' uses many_to_many=True but did not "
                        "provide related_name."
                    )
                metadata = ManyToManyRelation(
                    related_name=related_name,
                    through=relationship_payload.get("through"),
                    reverse_index=relationship_payload.get("reverse_index", True),
                )
                metadata.to = target
                local_relations[field_name] = metadata
                pending_relations.append((field_name, metadata))
                fields_to_remove.append(field_name)
                continue

            if isinstance(hint, str):
                hint = evaluate_foreign_key_annotation(
                    hint,
                    model=model_name,
                    field=field_name,
                    module=namespace.get("__module__", ""),
                    localns=namespace,
                    frame_ns=frame_ns,
                )
            # `Annotated[T, ForeignKey(...)] | None` is the same relation as
            # `Annotated[T | None, ForeignKey(...)]`.
            core = ModelMetaclass._strip_optional_union(hint)
            if get_origin(core) is Annotated:
                args = get_args(core)
                for metadata in args:
                    if isinstance(metadata, ForeignKey):
                        metadata.relation_annotation = (
                            args[0] if core is hint else Optional[args[0]]
                        )
                        inner = ModelMetaclass._strip_optional_union(args[0])
                        metadata.to = inner
                        if metadata.retired:
                            ModelMetaclass._retire_relation(
                                model_name, field_name, metadata, annotations, namespace
                            )
                            retired_relations[field_name] = metadata
                            break
                        local_relations[field_name] = metadata
                        pending_relations.append((field_name, metadata))
                        fields_to_remove.append(field_name)
                        break

                    if metadata.__class__.__name__ == "ManyToManyField":
                        raise TypeError(
                            "ManyToManyField(...) is no longer supported. Use "
                            "Relation[list[T]] = ManyToMany(...)."
                        )

        return local_relations, fields_to_remove, pending_relations, retired_relations

    @staticmethod
    def _retire_relation(
        model_name: str,
        field_name: str,
        metadata: ForeignKey,
        annotations: dict,
        namespace: dict,
    ) -> None:
        """Take a retired ``ForeignKey`` out of the class body (ADR-0053).

        The relation gets no descriptor and no shadow ``*_id`` field, so the
        model has neither attribute; its column is compiled from the
        ``ForeignKey`` alone (`ferro.columns.retired_column_specs`).
        """
        if metadata.renamed_from is not None:
            raise _retired_and_renamed(model_name, field_name)
        if not annotation_allows_none(metadata.relation_annotation):
            raise _retired_not_optional(model_name, field_name, metadata.to)
        if metadata.nullable is False:
            raise _retired_not_nullable(model_name, field_name)
        annotations.pop(field_name, None)
        namespace.pop(field_name, None)

    @staticmethod
    def _inject_shadow_fields(
        annotations: dict, namespace: dict, local_relations: dict
    ) -> None:
        """
        Inject shadow {field_name}_id fields for ForeignKeys.

        When ``ForeignKey.to`` is already a concrete model class, the shadow type is
        derived from that model's primary key annotation; otherwise a broad fallback
        union is used until ``resolve_relationships()`` can reconcile it.

        Mutates annotations and namespace in place.
        """
        for field_name, metadata in local_relations.items():
            if isinstance(metadata, ForeignKey):
                # INJECT SHADOW FIELD into annotations
                id_field = f"{field_name}_id"
                annotations[id_field] = shadow_annotation_for_foreign_key(metadata)
                # Set a default so Pydantic doesn't make it required
                namespace[id_field] = PydanticField(default=None)

    @staticmethod
    def _prepare_namespace_for_pydantic(
        namespace: dict, annotations: dict, fields_to_remove: list
    ) -> None:
        """
        Hide relationship fields from Pydantic by converting them to ClassVars.

        Mutates namespace and annotations in place.
        """
        # Hide relationship fields from Pydantic by converting them to ClassVars
        for field_name in fields_to_remove:
            annotations[field_name] = ClassVar[Any]

        # FOR PYTHON 3.14+: If we evaluated annotations, we MUST remove the func
        # so Pydantic doesn't use it and ignore our modified __annotations__.
        if "__annotate_func__" in namespace:
            del namespace["__annotate_func__"]

    @staticmethod
    def _register_model(cls, identity: str, local_relations: dict) -> None:
        """
        Register model in global registry and compute its queryable-column set.

        Raises:
            RuntimeError: When a *distinct* model already claims this model's
                resolved table name (FF-E E1). Re-registration under the same
                qualified identity is idempotent.
        """
        table_name = cls.__ferro_table__
        for key, other in REGISTRY.models().items():
            if key == identity:
                continue
            if getattr(other, "__ferro_table__", None) == table_name:
                raise RuntimeError(
                    f"Ferro model '{identity}' resolves to table '{table_name}', "
                    f"which is already registered by model '{key}'. Two distinct "
                    "models cannot share a table. Set __ferro_table__ on one of "
                    "them to give it a distinct table name."
                )
        REGISTRY.register(cls)
        cls.ferro_relations = local_relations

    @staticmethod
    def _parse_ferro_field_metadata(cls) -> dict[str, FerroField]:
        """
        Parse Ferro column metadata from model fields.

        Sources: ``Annotated[..., FerroField(...)]``, assignment ``Field(...)``
        (Ferro kwargs live under ``json_schema_extra["ferro_field"]``), and
        ``Annotated[..., Field(...)]`` (Pydantic merges that ``Field`` into the
        same ``FieldInfo`` shape as assignment).

        Returns:
            Dictionary mapping field names to FerroField metadata

        Raises:
            TypeError: If FerroField is declared twice for the same field
        """
        ferro_fields = {}
        for f_name, field_info in cls.model_fields.items():
            annotated_metadata: FerroField | None = None
            for metadata in field_info.metadata:
                if isinstance(metadata, FerroField):
                    annotated_metadata = metadata
                    break
            wrapped_metadata = None
            extra = getattr(field_info, "json_schema_extra", None)
            if isinstance(extra, dict):
                wrapped_payload = extra.get(FERRO_FIELD_EXTRA_KEY)
                if wrapped_payload:
                    field_payload = {
                        key: wrapped_payload[key]
                        for key in (
                            "primary_key",
                            "autoincrement",
                            "unique",
                            "index",
                            "nullable",
                            "db_type",
                            "db_check",
                            "renamed_from",
                            "retired",
                        )
                        if key in wrapped_payload
                    }
                    if field_payload:
                        wrapped_metadata = FerroField(**field_payload)
            generated = next(
                (
                    metadata.func
                    for metadata in field_info.metadata
                    if isinstance(metadata, GeneratedExpression)
                ),
                None,
            )
            if generated is not None:
                if annotated_metadata:
                    raise TypeError(
                        f"Field '{f_name}' cannot declare Ferro field metadata twice "
                        "(Annotated[..., FerroField(...)] + ferro.Field(generated=...)); "
                        "pass generated= to the one FerroField."
                    )
                wrapped_metadata = wrapped_metadata or FerroField()
                wrapped_metadata.generated = generated

            if annotated_metadata and wrapped_metadata:
                raise TypeError(
                    f"Field '{f_name}' cannot declare Ferro field metadata twice "
                    "(Annotated[...] + ferro.Field(...))."
                )

            if annotated_metadata:
                ferro_fields[f_name] = annotated_metadata
            elif wrapped_metadata:
                ferro_fields[f_name] = wrapped_metadata

        return ferro_fields

    @staticmethod
    def _register_enum_fields(cls) -> None:
        """Populate ``cls._enum_fields`` from column-spec python types."""
        enum_fields: dict[str, type[Enum]] = {}
        retired_labels: dict[str, tuple[type[Enum], frozenset[str]]] = {}
        for field_name, spec in cls.__ferro_columns__.items():
            enum_cls = spec.enum_class
            if enum_cls is not None:
                enum_fields[field_name] = enum_cls
                retired = declared_retired_labels(enum_cls)
                if retired and field_name in cls.model_fields:
                    retired_labels[field_name] = (enum_cls, retired)
        cls._enum_fields = enum_fields
        if retired_labels:
            ModelMetaclass._refuse_retired_labels(cls, retired_labels)

    @staticmethod
    def _register_hydration_converters(cls) -> None:
        """Populate ``cls._hydration_converters`` from the column specs.

        Hydration writes each column straight into the instance dict, never
        through ``__init__``, so a field whose declared type is not what the
        database hands back is converted, value by value, by the one entry
        recorded here: an enum field by its enum class, a secret field by a
        `SecretHydrator`. Each entry is ``(kind, convert)``; ``kind`` names the
        field in a rejection (``Failed to hydrate enum field ...``).
        """
        converters: dict[str, tuple[str, Callable[[Any], Any]]] = {}
        for field_name, spec in cls.__ferro_columns__.items():
            if spec.enum_class is not None:
                converters[field_name] = ("enum", spec.enum_class)
            elif spec.secret_class is not None:
                converters[field_name] = ("secret", SecretHydrator(spec.secret_class))
        cls._hydration_converters = converters

    @staticmethod
    def _refuse_retired_labels(
        cls, retired_labels: dict[str, tuple[type[Enum], frozenset[str]]]
    ) -> None:
        """Refuse writing a retired enum label, field by field (ADR-0053).

        Only the enum fields whose enum declares ``__ferro_retired_labels__``
        gain an ``AfterValidator``: it runs on construction and, under
        ``validate_assignment``, for the assigned field alone. Without
        ``validate_assignment`` the class gets a ``__setattr__`` for the same
        fields. Hydration runs neither, so a row holding the label loads and
        saves back unchanged. A model without retired labels gains nothing.
        """
        for field_name, (enum_cls, labels) in retired_labels.items():
            cls.model_fields[field_name].metadata.append(
                AfterValidator(partial(refuse_retired_label, enum_cls, labels))
            )
        cls.model_rebuild(force=True, raise_errors=False)
        if not cls.model_config.get("validate_assignment"):
            cls.__setattr__ = _guarded_setattr(
                frozenset(retired_labels), cls.__setattr__
            )

    @staticmethod
    def _validate_db_type_options(cls, ferro_fields: dict) -> None:
        """Strict validation of ``Field(db_type=..., db_check=...)`` combinations.

        Runs at class-definition time (Phase 3) so incoherent combinations
        surface as ``TypeError`` on import, never as a silent miscoercion at
        query or migration time. See U2 of the configurable-column-storage
        plan.
        """
        try:
            resolved = get_type_hints(cls, include_extras=True)
        except Exception:
            resolved = {}

        model_fields = getattr(cls, "model_fields", {})

        for field_name, metadata in ferro_fields.items():
            db_type = getattr(metadata, "db_type", None)
            db_check = getattr(metadata, "db_check", False)
            if db_type is None and not db_check:
                continue

            annotation = resolved.get(field_name)
            if annotation is None:
                field_info = model_fields.get(field_name)
                if field_info is not None:
                    annotation = field_info.annotation

            # Token validity + annotation compatibility validate path-blind at
            # spec compilation (ADR-0003; ``ferro.columns``). Only the
            # ferro-path-only db_check combinations live here.
            if db_check:
                if db_type is None:
                    raise TypeError(
                        f"Field '{field_name}' db_check=True requires db_type to be "
                        f"set. db_check on default (native enum) storage is redundant "
                        f"because the native enum type already enforces values."
                    )
                if annotation is not None and not is_closed_domain_annotation(
                    annotation
                ):
                    raise TypeError(
                        f"Field '{field_name}' db_check=True is only valid for "
                        f"closed-domain types (enum.Enum subclasses). "
                        f"Annotation {annotation!r} is not closed-domain."
                    )

    @staticmethod
    def _validate_generated_options(cls, ferro_fields: dict) -> None:
        """Refuse `generated=` beside an option the database cannot honour.

        A generated column's value comes only from its expression, so it has
        no key, sequence, default or enum check of its own.
        """
        for field_name, metadata in ferro_fields.items():
            if getattr(metadata, "generated", None) is None:
                continue
            field_info = cls.model_fields[field_name]
            conflicts = [
                option
                for option, declared in (
                    ("primary_key=True", metadata.primary_key),
                    ("autoincrement=True", metadata.autoincrement),
                    ("db_check=True", metadata.db_check),
                    (
                        "default=",
                        field_info.default is not None
                        and field_info.default is not PydanticUndefined
                        or field_info.default_factory is not None,
                    ),
                )
                if declared
            ]
            if conflicts:
                raise TypeError(
                    f"{cls.__name__}.{field_name} is a generated column, so it "
                    f"cannot also take {conflicts[0]}: the database computes "
                    f"its value from the expression; drop {conflicts[0]} or "
                    "generated="
                )

    @staticmethod
    def _inject_relation_descriptors(cls, local_relations: dict) -> None:
        """
        Inject descriptors for relationship fields (ForeignKey, ManyToMany).

        Mutates cls in place.
        """
        for field_name, metadata in local_relations.items():
            if isinstance(metadata, ForeignKey):
                target_name = (
                    metadata.to
                    if isinstance(metadata.to, str)
                    else (
                        metadata.to.__name__
                        if hasattr(metadata.to, "__name__")
                        else str(metadata.to)
                    )
                )
                if isinstance(metadata.to, ForwardRef):
                    target_name = metadata.to.__forward_arg__
                target_name = (
                    metadata.to.__ferro_identity__
                    if hasattr(metadata.to, "__ferro_identity__")
                    else target_name
                )

                setattr(
                    cls,
                    field_name,
                    ForwardDescriptor(
                        target_model_name=target_name,
                        field_name=field_name,
                    ),
                )
            else:
                setattr(cls, field_name, None)

    @staticmethod
    def _generate_and_register_schema(cls, name: str) -> None:
        """Build column specs and persist the SchemaIR envelope.

        Sets ``cls.__ferro_columns__`` (via the compile choke point). Rust
        registration is deferred to the bulk install seam at connect time (#246).

        Raises:
            RuntimeError: If spec build or registration fails
        """
        try:
            compile_model_schema_ir(name, cls)
        except TypeError:
            # Declaration errors (db_type validation, ADR-0003) are the
            # class-definition contract — surface unwrapped, never disguised
            # as a registration failure.
            raise
        except Exception as e:
            raise RuntimeError(f"Ferro failed to register model '{name}': {e}")
