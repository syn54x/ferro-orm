//! Zero-copy model hydration (AGENTS.md I-2).
//!
//! Builds Pydantic model instances by writing `__dict__` and required Pydantic slots directly,
//! without calling `BaseModel.__init__`. The authoritative list of slots the Rust core
//! initializes is [`HANDLED_BASEMODEL_SLOTS`]; an import-time guard
//! (`verify_pydantic_slot_abi`) refuses to load if the installed pydantic's `BaseModel.__slots__`
//! contains anything not in that list.

use crate::state::RustValue;
use pyo3::prelude::*;
use std::collections::HashMap;

/// Every `BaseModel.__slots__` entry the zero-copy hydrator accounts for.
///
/// Single source of truth for the hydration ABI (FF-G G1): the import-time
/// guard (`verify_pydantic_slot_abi`) diffs live `BaseModel.__slots__`
/// against this list, and `init_handled_slots` iterates it to initialize
/// each slot — the guard and the hydrator cannot drift apart because they
/// read the same const. A slot pydantic adds fails the guard at import; a
/// slot added here without a matching initializer arm fails loudly on the
/// first hydrate.
pub(crate) const HANDLED_BASEMODEL_SLOTS: &[&str] = &[
    "__dict__",
    "__pydantic_fields_set__",
    "__pydantic_extra__",
    "__pydantic_private__",
];

/// Resolve a model's enum-typed fields to their Python enum classes.
///
/// Built once per fetch (not per row) from `cls._enum_fields`, which the
/// metaclass populates from resolved annotations. The model class itself is
/// the registry holding the enum classes (FF-C C4), so no Python object ever
/// enters static Rust state.
pub fn enum_classes_for<'py>(
    py: Python<'py>,
    cls: &Bound<'py, PyAny>,
) -> HashMap<String, Bound<'py, PyAny>> {
    let mut out = HashMap::new();
    if let Ok(enum_fields) = cls.getattr(pyo3::intern!(py, "_enum_fields"))
        && let Ok(dict) = enum_fields.cast::<pyo3::types::PyDict>()
    {
        for (field_name, enum_cls) in dict.iter() {
            if let Ok(field_name) = field_name.extract::<String>() {
                out.insert(field_name, enum_cls);
            }
        }
    }
    out
}

/// Initialize every slot in [`HANDLED_BASEMODEL_SLOTS`] on a freshly
/// allocated instance, mirroring `BaseModel.__init__` slot assignment.
///
/// Driven by the const so the list and the initializers cannot drift: a
/// listed slot with no match arm is a loud error, never a silent skip.
fn init_handled_slots<'py>(
    py: Python<'py>,
    cls: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    fields_set: &Bound<'py, pyo3::types::PySet>,
) -> PyResult<()> {
    for slot in HANDLED_BASEMODEL_SLOTS {
        match *slot {
            // Materialized by the field writes in `apply_decoded_fields`.
            "__dict__" => {}
            "__pydantic_fields_set__" => {
                instance.setattr(pyo3::intern!(py, "__pydantic_fields_set__"), fields_set)?;
            }
            "__pydantic_extra__" => {
                let model_config = cls.getattr(pyo3::intern!(py, "model_config"))?;
                let extra_policy = model_config.call_method1(
                    pyo3::intern!(py, "get"),
                    (pyo3::intern!(py, "extra"), pyo3::intern!(py, "ignore")),
                )?;
                let extra_slot = if extra_policy.eq(pyo3::intern!(py, "allow"))? {
                    pyo3::types::PyDict::new(py).into_any().unbind()
                } else {
                    py.None()
                };
                instance.setattr(pyo3::intern!(py, "__pydantic_extra__"), extra_slot)?;
            }
            "__pydantic_private__" => {
                instance.setattr(pyo3::intern!(py, "__pydantic_private__"), py.None())?;
            }
            other => {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "HANDLED_BASEMODEL_SLOTS lists '{other}' but init_handled_slots has no \
                     initializer for it — add a match arm"
                )));
            }
        }
    }
    Ok(())
}

/// Hydrate a model instance from pre-decoded column values.
///
/// Allocates via `cls.__new__(cls)`, writes fields into `__dict__`, sets
/// `__ferro_connection_name` and the `__ferro_persisted` marker, and
/// initializes Pydantic tracking slots.
///
/// # Arguments
/// * `py` — Active Python interpreter token.
/// * `cls` — Model class object (e.g. `User`).
/// * `connection_name` — Registered connection name stored on the instance for routing.
/// * `fields` — `(column_name, decoded_value)` pairs in query result order.
/// * `py_col_names` — Interned `PyString` handles for column names (avoids per-row allocation).
/// * `enum_classes` — Enum class per enum-typed field, from [`enum_classes_for`];
///   non-null values of those fields hydrate as enum members (FF-C C4).
///
/// # Returns
/// A bound model instance with `__pydantic_fields_set__` populated for assigned columns.
///
/// # Errors
/// Returns `PyErr` if `__new__`, dict/slot assignment, or `RustValue` → Python conversion
/// fails, or if an enum column holds a value its enum class does not accept.
pub fn hydrate_model_instance<'py>(
    py: Python<'py>,
    cls: &Bound<'py, PyAny>,
    connection_name: &str,
    fields: Vec<(String, RustValue)>,
    py_col_names: &HashMap<String, pyo3::Py<pyo3::types::PyString>>,
    enum_classes: &HashMap<String, Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let instance = cls.call_method1(pyo3::intern!(py, "__new__"), (cls,))?;
    let dict_attr = instance.getattr(pyo3::intern!(py, "__dict__"))?;
    let dict = dict_attr.cast::<pyo3::types::PyDict>()?;
    dict.set_item(
        pyo3::intern!(py, "__ferro_connection_name"),
        connection_name,
    )?;
    // Hydrated instances are persistent by definition: `Model.save()` reads
    // this marker to choose UPDATE ... WHERE pk = ? over INSERT (FF-A A4).
    dict.set_item(pyo3::intern!(py, "__ferro_persisted"), true)?;
    let fields_set = pyo3::types::PySet::empty(py)?;

    apply_decoded_fields(
        py,
        cls,
        dict,
        &fields_set,
        fields,
        py_col_names,
        enum_classes,
    )?;

    init_handled_slots(py, cls, &instance, &fields_set)?;
    Ok(instance)
}

/// Hydrate a projected record (`Row`) from pre-decoded column values (#279).
///
/// The record flavor of [`hydrate_model_instance`]: same allocation via
/// `cls.__new__(cls)`, same decoded-field write path
/// ([`apply_decoded_fields`]), no `BaseModel.__init__`, no pydantic-core
/// validation (I-2). Differences, per ADR-0007:
///
/// - Decoded fields land in `__pydantic_extra__`, not `__dict__`: a `Row`
///   declares no static fields, and pydantic serializes/dumps extras (its
///   `model_config` is `extra="allow"`), so `model_dump()` sees exactly the
///   projected columns in selection order.
/// - NO persistence identity: no `__ferro_connection_name`, no
///   `__ferro_persisted` marker — a record can never masquerade as a row,
///   and it never enters the identity map (callers must not insert it).
///
/// Slot initialization is driven by [`HANDLED_BASEMODEL_SLOTS`] like
/// [`init_handled_slots`], so the record path fails as loudly as the model
/// path if pydantic grows a slot this build does not handle.
///
/// # Errors
/// Returns `PyErr` on allocation/slot failure, `RustValue` conversion
/// failure, or an enum column value its enum class rejects.
pub fn hydrate_record_instance<'py>(
    py: Python<'py>,
    record_cls: &Bound<'py, PyAny>,
    fields: Vec<(String, RustValue)>,
    py_col_names: &HashMap<String, pyo3::Py<pyo3::types::PyString>>,
    enum_classes: &HashMap<String, Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let instance = record_cls.call_method1(pyo3::intern!(py, "__new__"), (record_cls,))?;
    let extra = pyo3::types::PyDict::new(py);
    let fields_set = pyo3::types::PySet::empty(py)?;

    apply_decoded_fields(
        py,
        record_cls,
        &extra,
        &fields_set,
        fields,
        py_col_names,
        enum_classes,
    )?;

    for slot in HANDLED_BASEMODEL_SLOTS {
        match *slot {
            // A Row's own __dict__ stays empty: projected values are pydantic
            // extras, not declared fields.
            "__dict__" => {}
            "__pydantic_fields_set__" => {
                instance.setattr(pyo3::intern!(py, "__pydantic_fields_set__"), &fields_set)?;
            }
            "__pydantic_extra__" => {
                instance.setattr(pyo3::intern!(py, "__pydantic_extra__"), &extra)?;
            }
            "__pydantic_private__" => {
                instance.setattr(pyo3::intern!(py, "__pydantic_private__"), py.None())?;
            }
            other => {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "HANDLED_BASEMODEL_SLOTS lists '{other}' but hydrate_record_instance has \
                     no initializer for it — add a match arm"
                )));
            }
        }
    }
    Ok(instance)
}

/// Write decoded column values into `dict` and record them in `fields_set`.
///
/// The single materialization path for both fresh hydration and fetch-hit
/// refresh (FF-D D1b) — refresh cannot drift from hydration, and a partial
/// write is structurally impossible.
fn apply_decoded_fields<'py>(
    py: Python<'py>,
    cls: &Bound<'py, PyAny>,
    dict: &Bound<'py, pyo3::types::PyDict>,
    fields_set: &Bound<'py, pyo3::types::PySet>,
    fields: Vec<(String, RustValue)>,
    py_col_names: &HashMap<String, pyo3::Py<pyo3::types::PyString>>,
    enum_classes: &HashMap<String, Bound<'py, PyAny>>,
) -> PyResult<()> {
    for (col_name, val) in fields {
        let mut py_val = val.into_py_any(py)?;
        if let Some(enum_cls) = enum_classes.get(&col_name)
            && !py_val.is_none()
        {
            // One plan-consistent conversion at the hydration boundary; a
            // value the enum class rejects is real data corruption and must
            // surface, not be silently passed through (FF-C C4, I-6).
            py_val = enum_cls.call1((py_val,)).map_err(|err| {
                let model = cls
                    .getattr(pyo3::intern!(py, "__name__"))
                    .and_then(|n| n.extract::<String>())
                    .unwrap_or_else(|_| "<model>".to_string());
                pyo3::exceptions::PyValueError::new_err(format!(
                    "Failed to hydrate enum field {model}.{col_name}: {err}"
                ))
            })?;
        }
        if let Some(py_name) = py_col_names.get(&col_name) {
            let py_name = py_name.bind(py);
            dict.set_item(py_name, py_val)?;
            fields_set.add(py_name)?;
        } else {
            let py_name = pyo3::types::PyString::new(py, &col_name);
            dict.set_item(&py_name, py_val)?;
            fields_set.add(&py_name)?;
        }
    }
    Ok(())
}

/// Refresh a live cached instance from a freshly decoded row (FF-D D1b).
///
/// Overwrites every decoded field and resets `__pydantic_fields_set__` to
/// match a fresh hydration. Ferro markers (`__ferro_connection_name`,
/// `__ferro_persisted`) and pydantic extra/private slots are already present
/// on the instance and are left untouched.
///
/// # Precondition
/// `fields` must be a FULL row decode — every persisted column, not a
/// projected subset. All three current callers (`fetch_all`, `fetch_one`,
/// `fetch_filtered` in `src/operations.rs`) decode `SELECT table.*`, so this
/// holds today. A future caller that passes a partial-column decode would
/// silently corrupt the cached instance: columns absent from `fields` keep
/// their stale `__dict__` values, and `__pydantic_fields_set__` is reset to
/// only the decoded subset, understating which fields are actually set. This
/// function has no way to detect a partial decode from its arguments alone —
/// any partial-refresh caller must be rejected at the call site, not here.
pub fn refresh_model_instance<'py>(
    py: Python<'py>,
    cls: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    fields: Vec<(String, RustValue)>,
    py_col_names: &HashMap<String, pyo3::Py<pyo3::types::PyString>>,
    enum_classes: &HashMap<String, Bound<'py, PyAny>>,
) -> PyResult<()> {
    let dict_attr = instance.getattr(pyo3::intern!(py, "__dict__"))?;
    let dict = dict_attr.cast::<pyo3::types::PyDict>()?;
    let fields_set = pyo3::types::PySet::empty(py)?;
    apply_decoded_fields(
        py,
        cls,
        dict,
        &fields_set,
        fields,
        py_col_names,
        enum_classes,
    )?;
    instance.setattr(pyo3::intern!(py, "__pydantic_fields_set__"), fields_set)?;
    Ok(())
}

/// Populate a forward-FK relation on a hydrated instance (#286, ADR-0008).
///
/// The population contract: the relation field name enters the instance's
/// `__dict__`, shadowing the class-level non-data `ForwardDescriptor`, so
/// access is a plain attribute returning the complete related instance —
/// no await, no query. `value: None` is a *populated* `None` (a nullable FK
/// with no target): the key is present, so access returns `None` instead of
/// the awaitable. An instance with no entry under the relation name keeps
/// today's awaitable contract untouched.
pub fn set_populated_relation<'py>(
    py: Python<'py>,
    instance: &Bound<'py, PyAny>,
    relation: &str,
    value: Option<&Bound<'py, PyAny>>,
) -> PyResult<()> {
    let dict_attr = instance.getattr(pyo3::intern!(py, "__dict__"))?;
    let dict = dict_attr.cast::<pyo3::types::PyDict>()?;
    match value {
        Some(obj) => dict.set_item(relation, obj)?,
        None => dict.set_item(relation, py.None())?,
    }
    Ok(())
}

/// Diff a class's `__slots__` against [`HANDLED_BASEMODEL_SLOTS`].
///
/// # Errors
/// Returns a `PyErr` naming every unknown slot and the running pydantic
/// version when the class declares a slot the hydrator does not initialize.
fn verify_slots_handled(cls: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = cls.py();
    let slots: Vec<String> = cls.getattr(pyo3::intern!(py, "__slots__"))?.extract()?;
    let unknown: Vec<&str> = slots
        .iter()
        .map(String::as_str)
        .filter(|slot| !HANDLED_BASEMODEL_SLOTS.contains(slot))
        .collect();
    if unknown.is_empty() {
        return Ok(());
    }
    let version = py
        .import("pydantic")
        .and_then(|m| m.getattr("VERSION"))
        .and_then(|v| v.extract::<String>())
        .unwrap_or_else(|_| "unknown".to_string());
    Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
        "ferro's zero-copy hydration does not know how to initialize pydantic BaseModel \
         slot(s) {unknown:?} (pydantic {version}). This ferro build supports: \
         {HANDLED_BASEMODEL_SLOTS:?}. Your pydantic version is likely newer than this \
         ferro build supports — pin pydantic to a supported minor or upgrade ferro."
    )))
}

/// Import-time hydration ABI guard (FF-G G1): refuse to load `_core` when
/// pydantic's `BaseModel.__slots__` contains a slot the hydrator would leave
/// uninitialized — a loud startup error instead of silent breakage on the
/// next pydantic minor.
///
/// # Errors
/// Returns a `PyErr` if pydantic cannot be imported or declares an unknown slot.
pub(crate) fn verify_pydantic_slot_abi(py: Python<'_>) -> PyResult<()> {
    let base_model = py.import("pydantic")?.getattr("BaseModel")?;
    verify_slots_handled(&base_model)
}

/// Test-only: run the hydration ABI diff against an arbitrary class.
///
/// # Errors
/// Returns a `PyErr` when the class declares a slot the hydrator does not handle.
#[pyfunction]
pub fn _verify_hydration_abi_for_test(cls: Bound<'_, PyAny>) -> PyResult<()> {
    verify_slots_handled(&cls)
}
