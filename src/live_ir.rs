//! The live converter: a connected database read into the planner's input.
//!
//! Everything the one planner (`ferro_migrate::plan_from_ir`) learns about a
//! live database comes through here, as one `SchemaIrPayload` envelope — the
//! tables, their columns, ferro-owned indexes and foreign keys, in the IR's
//! own vocabulary — plus a [`LiveFacts`] side-table for what the IR cannot
//! say: CHECK bodies and row policies as the catalog prints them, constraint
//! and index validity, and every live enum type's labels. The reconciliation
//! pass, the drift check and the Alembic bridge all read the database this
//! way, so they plan against the same picture of it.

use crate::backend::EngineHandle;
use crate::introspect::{
    LiveCheck, LiveColumn, LiveForeignKey, LiveIndex, live_enum_type_labels, live_table_checks,
    live_table_columns, live_table_foreign_keys, live_table_indexes, live_table_names,
    live_table_row_security,
};
use ferro_ddl_lowering::{Dialect, LiveRowSecurity, information_schema_to_db_type_token};
use ferro_migrate::{LiveCheckFact, LiveFacts, LiveFkValidity, LiveIndexValidity, LiveTableFacts};
use ferro_schema_ir::{
    IrEnvelope, SchemaColumn, SchemaForeignKey, SchemaIndex, SchemaIrPayload, SchemaModel,
};
use pyo3::prelude::*;
use std::collections::BTreeMap;

/// One live table as introspection reports it.
#[derive(Clone, Debug, Default)]
pub(crate) struct LiveTable {
    pub name: String,
    pub columns: Vec<LiveColumn>,
    pub indexes: Vec<LiveIndex>,
    pub foreign_keys: Vec<LiveForeignKey>,
    pub checks: Vec<LiveCheck>,
    pub row_security: LiveRowSecurity,
}

/// Convert introspected tables (and the live enum types' labels) into the
/// planner's input: one envelope with a model per table, in table-name
/// order, and the side-table of live facts.
pub(crate) fn live_tables_to_schema_ir(
    tables: Vec<LiveTable>,
    enum_labels: BTreeMap<String, Vec<String>>,
    dialect: Dialect,
) -> (IrEnvelope<SchemaIrPayload>, LiveFacts) {
    let mut tables = tables;
    tables.sort_by(|a, b| a.name.cmp(&b.name));
    let mut facts = LiveFacts {
        tables: BTreeMap::new(),
        enum_labels,
    };
    let models = tables
        .into_iter()
        .map(|table| {
            facts.tables.insert(table.name.clone(), table_facts(&table));
            live_table_model(table, dialect)
        })
        .collect();
    let envelope = IrEnvelope {
        ir_kind: "schema".to_string(),
        ir_version: 1,
        payload: SchemaIrPayload {
            dialect_agnostic: true,
            models,
        },
    };
    (envelope, facts)
}

/// The IR a live table reads as: its columns with the storage token
/// introspection reports (`logical_type` unknown — the token decides), its
/// ferro-owned indexes and its foreign keys. CHECKs and row security are
/// facts, not IR: their bodies are the catalog's rendering.
fn live_table_model(table: LiveTable, dialect: Dialect) -> SchemaModel {
    let mut columns: Vec<SchemaColumn> = table
        .columns
        .iter()
        .map(|col| SchemaColumn {
            renamed_from: None,
            name: col.name.clone(),
            logical_type: "unknown".to_string(),
            db_type: Some(information_schema_to_db_type_token(
                &col.declared_type,
                col.char_max_len,
                dialect,
            )),
            db_type_explicit: None,
            nullable: col.is_nullable,
            primary_key: col.is_primary_key,
            autoincrement: false,
            unique: false,
            index: false,
            default: None,
            format: None,
            enum_values: None,
            enum_type_name: None,
            postgres_native_enum: col.is_enum_udt,
            enum_renamed_labels: Default::default(),
        })
        .collect();
    columns.sort_by(|a, b| a.name.cmp(&b.name));
    let mut foreign_keys: Vec<SchemaForeignKey> = table
        .foreign_keys
        .iter()
        .map(|fk| SchemaForeignKey {
            renamed_from: None,
            column: fk.column.clone(),
            to_table: fk.to_table.clone(),
            to_column: fk.to_column.clone(),
            on_delete: Some(fk.on_delete.clone()),
            name: fk.name.clone(),
        })
        .collect();
    foreign_keys.sort_by(|a, b| a.column.cmp(&b.column));
    SchemaModel {
        renamed_from: None,
        model_name: table.name.clone(),
        table_name: table.name,
        columns,
        foreign_keys,
        indexes: table
            .indexes
            .iter()
            .map(|index| SchemaIndex {
                name: index.name.clone(),
                columns: index.columns.clone(),
                unique: index.unique,
            })
            .collect(),
        uniques: Vec::new(),
        checks: Vec::new(),
        table_checks: Vec::new(),
        row_security: None,
    }
}

fn table_facts(table: &LiveTable) -> LiveTableFacts {
    LiveTableFacts {
        checks: table
            .checks
            .iter()
            .map(|check| LiveCheckFact {
                name: check.name.clone(),
                definition: check.definition.clone(),
                ferro_owned: check.ferro_owned,
                validated: check.validated,
            })
            .collect(),
        foreign_keys: table
            .foreign_keys
            .iter()
            .filter_map(|fk| {
                Some(LiveFkValidity {
                    name: fk.name.clone()?,
                    validated: fk.validated,
                })
            })
            .collect(),
        indexes: table
            .indexes
            .iter()
            .map(|index| LiveIndexValidity {
                name: index.name.clone(),
                valid: index.valid,
            })
            .collect(),
        row_security: table.row_security.clone(),
    }
}

/// Whether a live table is the engine's own bookkeeping rather than schema:
/// SQLite reserves every `sqlite_`-prefixed name (`sqlite_sequence`,
/// `sqlite_stat1`, …) for internal use, and no model can own one.
fn is_engine_internal_table(name: &str, dialect: Dialect) -> bool {
    dialect == Dialect::Sqlite && name.starts_with("sqlite_")
}

/// Read the live database behind `engine` into the planner's input: every
/// table named in `tables` that exists (every live schema table when `None`
/// — SQLite's internal `sqlite_*` tables are not schema), plus every live
/// enum type's labels on Postgres.
///
/// # Errors
/// A `PyErr` when an introspection query fails.
pub async fn live_schema_ir(
    engine: &EngineHandle,
    tables: Option<&[String]>,
) -> PyResult<(IrEnvelope<SchemaIrPayload>, LiveFacts)> {
    let names: Vec<String> = match tables {
        Some(names) => names.to_vec(),
        None => {
            let mut names: Vec<String> = live_table_names(engine)
                .await?
                .into_iter()
                .filter(|name| !is_engine_internal_table(name, engine.backend()))
                .collect();
            names.sort();
            names
        }
    };
    let mut live = Vec::with_capacity(names.len());
    for name in names {
        // A table that does not exist reads as absent, so the planner plans
        // it as an add.
        let Some(columns) = live_table_columns(engine, &name).await? else {
            continue;
        };
        live.push(LiveTable {
            columns,
            indexes: live_table_indexes(engine, &name).await?,
            foreign_keys: live_table_foreign_keys(engine, &name).await?,
            checks: live_table_checks(engine, &name).await?,
            row_security: live_table_row_security(engine, &name).await?,
            name,
        });
    }
    let dialect = engine.backend();
    let enum_labels = if dialect == Dialect::Postgres {
        live_enum_type_labels(engine).await?
    } else {
        BTreeMap::new()
    };
    Ok(live_tables_to_schema_ir(live, enum_labels, dialect))
}

/// Read the database behind connection `using` into an IR envelope and its
/// live facts, as JSON: `(ir_json, facts_json)`. `tables_json` is a JSON list
/// of table names to read; `None` reads every live table.
///
/// # Errors
/// A `PyErr` when `tables_json` is not a JSON list of strings, the connection
/// is not open, or introspection fails.
#[pyfunction]
#[pyo3(name = "_live_schema_ir")]
#[pyo3(signature = (using=None, tables_json=None))]
pub fn _live_schema_ir(
    py: Python<'_>,
    using: Option<String>,
    tables_json: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    let tables: Option<Vec<String>> = tables_json
        .map(|json| {
            serde_json::from_str(&json).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!(
                    "tables_json must be a JSON list of table names: {e}"
                ))
            })
        })
        .transpose()?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        let (envelope, facts) = live_schema_ir(&engine, tables.as_deref()).await?;
        let to_json = |value: serde_json::Result<String>| {
            value.map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "could not serialize the live schema: {e}"
                ))
            })
        };
        Ok((
            to_json(serde_json::to_string(&envelope))?,
            to_json(serde_json::to_string(&facts))?,
        ))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use ferro_ddl_lowering::LiveRowPolicy;

    fn column(name: &str, declared_type: &str, nullable: bool, pk: bool) -> LiveColumn {
        LiveColumn {
            name: name.to_string(),
            declared_type: declared_type.to_string(),
            is_nullable: nullable,
            is_primary_key: pk,
            char_max_len: None,
            is_enum_udt: false,
        }
    }

    fn post() -> LiveTable {
        LiveTable {
            name: "post".to_string(),
            columns: vec![
                column("title", "character varying", true, false),
                column("id", "integer", false, true),
                column("author_id", "integer", true, false),
            ],
            indexes: vec![LiveIndex {
                name: "uq_post_title".to_string(),
                columns: vec!["title".to_string()],
                unique: true,
                valid: false,
            }],
            foreign_keys: vec![LiveForeignKey {
                name: Some("fk_post_author_id_author".to_string()),
                column: "author_id".to_string(),
                to_table: "author".to_string(),
                to_column: "id".to_string(),
                on_delete: "CASCADE".to_string(),
                validated: false,
            }],
            checks: vec![LiveCheck {
                name: "ck_post_title_set".to_string(),
                definition: "CHECK ((title IS NOT NULL)) NOT VALID".to_string(),
                ferro_owned: true,
                validated: false,
            }],
            row_security: LiveRowSecurity {
                enabled: true,
                forced: false,
                policies: vec![LiveRowPolicy {
                    name: "dba_fence".to_string(),
                    command: "all".to_string(),
                    using: Some("true".to_string()),
                    ..LiveRowPolicy::default()
                }],
            },
        }
    }

    #[test]
    fn a_live_table_becomes_one_model_with_its_columns_indexes_and_foreign_keys() {
        let author = LiveTable {
            name: "author".to_string(),
            columns: vec![column("id", "integer", false, true)],
            ..LiveTable::default()
        };
        let (envelope, _) =
            live_tables_to_schema_ir(vec![post(), author], BTreeMap::new(), Dialect::Postgres);
        assert_eq!(envelope.ir_kind, "schema");
        let tables: Vec<&str> = envelope
            .payload
            .models
            .iter()
            .map(|model| model.table_name.as_str())
            .collect();
        assert_eq!(tables, vec!["author", "post"], "table-name order");
        let post = &envelope.payload.models[1];
        let columns: Vec<(&str, Option<&str>, bool, bool)> = post
            .columns
            .iter()
            .map(|c| {
                (
                    c.name.as_str(),
                    c.db_type.as_deref(),
                    c.nullable,
                    c.primary_key,
                )
            })
            .collect();
        assert_eq!(
            columns,
            vec![
                ("author_id", Some("int"), true, false),
                ("id", Some("int"), false, true),
                ("title", Some("varchar"), true, false),
            ]
        );
        assert_eq!(post.indexes.len(), 1);
        assert_eq!(post.indexes[0].name, "uq_post_title");
        assert_eq!(
            post.foreign_keys[0].name.as_deref(),
            Some("fk_post_author_id_author")
        );
        assert_eq!(post.foreign_keys[0].on_delete.as_deref(), Some("CASCADE"));
        assert!(post.checks.is_empty() && post.table_checks.is_empty());
        assert!(post.row_security.is_none(), "row security is a live fact");
    }

    #[test]
    fn what_the_ir_cannot_say_travels_as_live_facts() {
        let mut labels = BTreeMap::new();
        labels.insert("status".to_string(), vec!["draft".to_string()]);
        let (_, facts) = live_tables_to_schema_ir(vec![post()], labels.clone(), Dialect::Postgres);
        assert_eq!(facts.enum_labels, labels);
        let post = &facts.tables["post"];
        assert_eq!(
            post.checks,
            vec![LiveCheckFact {
                name: "ck_post_title_set".to_string(),
                definition: "CHECK ((title IS NOT NULL)) NOT VALID".to_string(),
                ferro_owned: true,
                validated: false,
            }]
        );
        assert_eq!(
            post.foreign_keys,
            vec![LiveFkValidity {
                name: "fk_post_author_id_author".to_string(),
                validated: false,
            }]
        );
        assert_eq!(
            post.indexes,
            vec![LiveIndexValidity {
                name: "uq_post_title".to_string(),
                valid: false,
            }]
        );
        assert_eq!(post.row_security.policies[0].name, "dba_fence");
        assert!(!post.row_security.policies[0].ferro_owned);
    }

    #[test]
    fn sqlite_bookkeeping_tables_are_not_schema() {
        assert!(is_engine_internal_table("sqlite_sequence", Dialect::Sqlite));
        assert!(!is_engine_internal_table("post", Dialect::Sqlite));
        assert!(!is_engine_internal_table(
            "sqlite_sequence",
            Dialect::Postgres
        ));
    }

    #[test]
    fn an_unnamed_sqlite_foreign_key_has_no_validity_fact() {
        let mut table = post();
        table.foreign_keys[0].name = None;
        let (envelope, facts) =
            live_tables_to_schema_ir(vec![table], BTreeMap::new(), Dialect::Sqlite);
        assert!(facts.tables["post"].foreign_keys.is_empty());
        assert_eq!(envelope.payload.models[0].foreign_keys[0].name, None);
    }
}
