use crate::state::RustValue;
use pyo3::prelude::*;
use std::collections::HashMap;

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
