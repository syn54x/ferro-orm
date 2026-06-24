//! Zero-copy model hydration (AGENTS.md I-2).
//!
//! Builds Pydantic model instances by writing `__dict__` and required Pydantic slots directly,
//! without calling `BaseModel.__init__`. The Rust core must initialize every slot in
//! `BaseModel.__slots__` that `__init__` would set (`__pydantic_fields_set__`,
//! `__pydantic_extra__`, `__pydantic_private__`).

use crate::state::RustValue;
use pyo3::prelude::*;
use std::collections::HashMap;

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
/// `__ferro_connection_name`, and initializes Pydantic tracking slots.
///
/// # Arguments
/// * `py` — Active Python interpreter token.
/// * `cls` — Model class object (e.g. `User`).
/// * `connection_name` — Registered connection name stored on the instance for routing.
/// * `fields` — `(column_name, decoded_value)` pairs in query result order.
/// * `py_col_names` — Interned `PyString` handles for column names (avoids per-row allocation).
///
/// # Returns
/// A bound model instance with `__pydantic_fields_set__` populated for assigned columns.
///
/// # Errors
/// Returns `PyErr` if `__new__`, dict/slot assignment, or `RustValue` → Python conversion fails.
pub fn hydrate_model_instance<'py>(
    py: Python<'py>,
    cls: &Bound<'py, PyAny>,
    connection_name: &str,
    fields: Vec<(String, RustValue)>,
    py_col_names: &HashMap<String, pyo3::Py<pyo3::types::PyString>>,
) -> PyResult<Bound<'py, PyAny>> {
    let instance = cls.call_method1(pyo3::intern!(py, "__new__"), (cls,))?;
    let dict_attr = instance.getattr(pyo3::intern!(py, "__dict__"))?;
    let dict = dict_attr.cast::<pyo3::types::PyDict>()?;
    dict.set_item(
        pyo3::intern!(py, "__ferro_connection_name"),
        connection_name,
    )?;
    let fields_set = pyo3::types::PySet::empty(py)?;

    for (col_name, val) in fields {
        let py_val = val.into_py_any(py)?;
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

    let _ = instance.setattr(pyo3::intern!(py, "__pydantic_fields_set__"), fields_set);
    set_pydantic_hydration_slots(py, cls, &instance)?;
    Ok(instance)
}
