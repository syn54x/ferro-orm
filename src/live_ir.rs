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
use ferro_migrate::plan::live_table_hints;
use ferro_migrate::{
    Hint, HintError, LiveCheckFact, LiveFacts, LiveFkValidity, LiveIndexValidity, LiveTableFacts,
};
use ferro_schema_ir::{
    IrEnvelope, SchemaColumn, SchemaForeignKey, SchemaIndex, SchemaIrPayload, SchemaModel,
};
use pyo3::prelude::*;
use std::collections::{BTreeMap, BTreeSet};

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
    let mut facts = LiveFacts::live(BTreeMap::new(), enum_labels);
    let models = tables
        .into_iter()
        .map(|table| {
            facts.tables.insert(table.name.clone(), table_facts(&table));
            live_table_model(table, &facts.enum_labels, dialect)
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
/// ferro-owned indexes and its foreign keys. A native-enum column names its
/// type and carries the type's live labels (`enum_labels`, in enum sort
/// order), so it resolves to the same `PgEnum` storage a declaration of that
/// type does — the shape a plan run back to the live database restores.
/// CHECKs and row security are facts, not IR: their bodies are the catalog's
/// rendering.
fn live_table_model(
    table: LiveTable,
    enum_labels: &BTreeMap<String, Vec<String>>,
    dialect: Dialect,
) -> SchemaModel {
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
            enum_values: native_enum_type(col)
                .and_then(|name| enum_labels.get(name))
                .map(|labels| labels.iter().cloned().map(serde_json::Value::String).collect()),
            enum_type_name: native_enum_type(col).map(str::to_string),
            postgres_native_enum: col.is_enum_udt,
            enum_renamed_labels: Default::default(),
            default_factory: None,
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

/// The native enum type a live column declares, when it is one.
fn native_enum_type(col: &LiveColumn) -> Option<&str> {
    col.enum_type_name.as_deref().filter(|_| col.is_enum_udt)
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

/// The live tables a read planned against `declared` covers: every declared
/// table that is live, plus the old table of each live table rename hint
/// `declared` carries (ADR-0032, decided by the planner's own liveness rule
/// through [`live_table_renames`], so the plan renames the table instead of
/// adding an empty one), minus `exclude`, plus every live table in `extra`
/// — sorted. A live table is what the one reader says
/// ([`live_table_names`]: a base table, never a view, never SQLite's own
/// `sqlite_*` tables), so a view named like a model reads as absent.
///
/// The reconciliation pass excludes the tables its create pass just built;
/// the Alembic bridge adds the tables a revision drops; `drift` and
/// `baseline` read `declared` (the snapshot) alone (ADR-0047).
///
/// # Errors
/// A `PyErr` when the catalog query fails.
pub async fn tables_to_read(
    engine: &EngineHandle,
    declared: &IrEnvelope<SchemaIrPayload>,
    exclude: &[String],
    extra: &[String],
) -> PyResult<Vec<String>> {
    let live: BTreeSet<String> = live_table_names(engine).await?.into_iter().collect();
    Ok(select_tables_to_read(
        &live,
        &declared.payload,
        exclude,
        extra,
    ))
}

/// [`tables_to_read`]'s rule over the live table names. A refused hint
/// ([`HintError`]) renames nothing, so nothing more is read for it; the
/// planner states the refusal.
fn select_tables_to_read(
    live: &BTreeSet<String>,
    declared: &SchemaIrPayload,
    exclude: &[String],
    extra: &[String],
) -> Vec<String> {
    let renamed = live_table_renames(live, declared).unwrap_or_default();
    let mut read: BTreeSet<String> = declared
        .models
        .iter()
        .map(|model| model.table_name.clone())
        .filter(|table| live.contains(table))
        .chain(renamed.into_iter().map(|(old, _)| old))
        .filter(|table| !exclude.contains(table))
        .collect();
    read.extend(extra.iter().filter(|table| live.contains(*table)).cloned());
    read.into_iter().collect()
}

/// Read the live tables named in `tables` ([`tables_to_read`]) into the
/// planner's input, plus every live enum type's labels on Postgres.
///
/// # Errors
/// A `PyErr` when an introspection query fails.
pub async fn live_schema_ir(
    engine: &EngineHandle,
    tables: &[String],
) -> PyResult<(IrEnvelope<SchemaIrPayload>, LiveFacts)> {
    let mut live = Vec::with_capacity(tables.len());
    for name in tables {
        // A table dropped since `tables_to_read` asked reads as absent, so
        // the planner plans it as an add.
        let Some(columns) = live_table_columns(engine, name).await? else {
            continue;
        };
        live.push(LiveTable {
            columns,
            indexes: live_table_indexes(engine, name).await?,
            foreign_keys: live_table_foreign_keys(engine, name).await?,
            checks: live_table_checks(engine, name).await?,
            row_security: live_table_row_security(engine, name).await?,
            name: name.clone(),
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

/// The live table renames `declared` asks of a database whose tables are
/// `live`, as `(old, new)`: one per live `__ferro_renamed_from__` hint
/// (ADR-0032), decided by the planner's own rule.
///
/// # Errors
/// The planner's refusal of a hint ([`HintError`]), under which nothing
/// renames.
pub fn live_table_renames(
    live: &BTreeSet<String>,
    declared: &SchemaIrPayload,
) -> Result<Vec<(String, String)>, HintError> {
    Ok(live_table_hints(live, declared)?
        .into_iter()
        .filter_map(|hint| match hint {
            Hint::Table { old, new } => Some((old, new)),
            _ => None,
        })
        .collect())
}

/// Read the database behind connection `using`, planned against
/// `declared_json` (a schema IR envelope), into an IR envelope and its live
/// facts, as JSON: `(ir_json, facts_json)`. The tables read are
/// [`tables_to_read`]'s, with `extra_tables_json` (a JSON list of table
/// names) as its extra tables.
///
/// # Errors
/// A `PyErr` when `declared_json` is not a schema IR envelope,
/// `extra_tables_json` is not a JSON list of strings, the connection is not
/// open, or introspection fails.
#[pyfunction]
#[pyo3(name = "_live_schema_ir")]
#[pyo3(signature = (using, declared_json, extra_tables_json=None))]
pub fn _live_schema_ir(
    py: Python<'_>,
    using: Option<String>,
    declared_json: String,
    extra_tables_json: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    let declared = crate::migrate::parse_schema_envelope(&declared_json, "declared_json")?;
    let extra: Vec<String> = extra_tables_json
        .map(|json| {
            serde_json::from_str(&json).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!(
                    "extra_tables_json must be a JSON list of table names: {e}"
                ))
            })
        })
        .transpose()?
        .unwrap_or_default();
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        let tables = tables_to_read(&engine, &declared, &[], &extra).await?;
        let (envelope, facts) = live_schema_ir(&engine, &tables).await?;
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
            enum_type_name: None,
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
    fn a_native_enum_column_names_its_type_and_carries_its_live_labels() {
        let mut status = column("status", "USER-DEFINED", false, false);
        status.is_enum_udt = true;
        status.enum_type_name = Some("poststatus".to_string());
        let table = LiveTable {
            name: "post".to_string(),
            columns: vec![column("id", "integer", false, true), status],
            ..LiveTable::default()
        };
        let mut labels = BTreeMap::new();
        labels.insert(
            "poststatus".to_string(),
            vec!["draft".to_string(), "live".to_string()],
        );
        let (envelope, _) = live_tables_to_schema_ir(vec![table], labels, Dialect::Postgres);
        let status = envelope.payload.models[0]
            .columns
            .iter()
            .find(|col| col.name == "status")
            .expect("status column");
        assert_eq!(status.enum_type_name.as_deref(), Some("poststatus"));
        assert!(status.postgres_native_enum);
        assert_eq!(
            ferro_ddl_lowering::resolve_column_storage(status, Dialect::Postgres),
            Ok(ferro_ddl_lowering::ResolvedStorage::PgEnum {
                type_name: "poststatus".to_string(),
                labels: vec!["draft".to_string(), "live".to_string()],
            })
        );
        let id = &envelope.payload.models[0].columns[0];
        assert!(id.enum_type_name.is_none() && id.enum_values.is_none());
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

    /// A declared modelset of `tables`, each `(table, renamed_from)`.
    fn declared(tables: &[(&str, Option<&str>)]) -> SchemaIrPayload {
        let (envelope, _) =
            live_tables_to_schema_ir(vec![post()], BTreeMap::new(), Dialect::Postgres);
        let mut payload = envelope.payload;
        let template = payload.models.remove(0);
        payload.models = tables
            .iter()
            .map(|(table, renamed_from)| SchemaModel {
                table_name: (*table).to_string(),
                renamed_from: renamed_from.map(str::to_string),
                ..template.clone()
            })
            .collect();
        payload
    }

    fn names(tables: &[&str]) -> BTreeSet<String> {
        tables.iter().map(|table| (*table).to_string()).collect()
    }

    #[test]
    fn a_read_covers_the_live_declared_tables_and_each_live_hints_old_table() {
        let declared = declared(&[("author", None), ("squad", Some("team")), ("tag", None)]);
        // `tag` is not live; `team` is, under `squad`'s hint; `audit` is an
        // undeclared live table nobody asked for.
        let live = names(&["author", "team", "audit"]);
        assert_eq!(
            select_tables_to_read(&live, &declared, &[], &[]),
            ["author", "team"]
        );
        // The pass excludes what its create pass built; the bridge adds what a
        // revision drops — a live table only.
        assert_eq!(
            select_tables_to_read(
                &live,
                &declared,
                &["author".to_string()],
                &["audit".to_string(), "gone".to_string()],
            ),
            ["audit", "team"]
        );
    }

    #[test]
    fn a_hint_whose_new_table_is_live_too_reads_no_old_table() {
        let declared = declared(&[("squad", Some("team"))]);
        let live = names(&["squad", "team"]);
        assert_eq!(select_tables_to_read(&live, &declared, &[], &[]), ["squad"]);
    }

    #[test]
    fn a_refused_hint_reads_nothing_more() {
        // Two tables claiming one old table: the planner refuses the hints.
        let declared = declared(&[("squad", Some("team")), ("crew", Some("team"))]);
        let live = names(&["team"]);
        assert!(live_table_renames(&live, &declared).is_err());
        assert!(select_tables_to_read(&live, &declared, &[], &[]).is_empty());
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
