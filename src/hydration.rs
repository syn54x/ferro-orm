//! Zero-copy model hydration (AGENTS.md I-2).
//!
//! Builds Pydantic model instances by writing `__dict__` and required Pydantic slots directly,
//! without calling `BaseModel.__init__`. The Rust core must initialize every slot in
//! `BaseModel.__slots__` that `__init__` would set (`__pydantic_fields_set__`,
//! `__pydantic_extra__`, `__pydantic_private__`).

use crate::state::RustValue;
use pyo3::prelude::*;
use std::collections::HashMap;

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

/// Initialize Pydantic v2 hydration slots on a freshly allocated instance.
///
/// Mirrors `BaseModel.__init__` slot assignment so attribute access on hydrated instances
/// matches conventionally constructed models.
fn set_pydantic_hydration_slots<'py>(
    py: Python<'py>,
    cls: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
) -> PyResult<()> {
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
    instance.setattr(pyo3::intern!(py, "__pydantic_private__"), py.None())?;
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

    let _ = instance.setattr(pyo3::intern!(py, "__pydantic_fields_set__"), fields_set);
    set_pydantic_hydration_slots(py, cls, &instance)?;
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
