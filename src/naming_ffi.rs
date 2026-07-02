//! FFI surface for the single-sourced DDL artifact-name builders.
//!
//! The Python IR compiler (`src/ferro/ir/compiler.py`) and the Alembic bridge
//! consume these instead of re-implementing the formats — the name rules and
//! their 63-char truncation guards live only in `ferro-ddl-lowering`
//! (AGENTS.md § I-1; FF-B B3).

use pyo3::prelude::*;

#[pyfunction]
pub fn _ddl_single_index_name(table: String, column: String) -> String {
    ferro_ddl_lowering::single_index_name(&table, &column)
}

#[pyfunction]
pub fn _ddl_single_unique_name(table: String, column: String) -> String {
    ferro_ddl_lowering::single_unique_index_name(&table, &column)
}

#[pyfunction]
pub fn _ddl_composite_index_name(table: String, columns: Vec<String>) -> String {
    let refs: Vec<&str> = columns.iter().map(String::as_str).collect();
    ferro_ddl_lowering::composite_index_name(&table, &refs)
}

#[pyfunction]
pub fn _ddl_composite_unique_name(table: String, columns: Vec<String>) -> String {
    let refs: Vec<&str> = columns.iter().map(String::as_str).collect();
    ferro_ddl_lowering::composite_unique_index_name(&table, &refs)
}

#[pyfunction]
pub fn _ddl_check_constraint_name(table: String, column: String) -> String {
    ferro_ddl_lowering::db_check_constraint_name(&table, &column)
}

#[pyfunction]
pub fn _ddl_fk_name(table: String, column: String, to_table: String) -> String {
    ferro_ddl_lowering::fk_name(&table, &column, &to_table)
}
