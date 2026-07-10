//! Query IR lowering to SeaQuery `Condition`s.
//!
//! Consumes canonical [`QueryIrPayload`] envelopes from `ferro_schema_ir` — the only
//! query shape in Rust (FF-F F-4) — and builds backend-aware filter SQL with typed
//! null/UUID/enum binds via [`crate::codec`].

use crate::state::Dialect;
use ferro_schema_ir::{QueryIrPayload, QueryNode, QueryOrderBy};
use sea_query::{Alias, Condition, Expr, SimpleExpr};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;

/// Reject relation-traversal shape this build cannot render yet (#269 defensive loudness).
///
/// Every leaf and `order_by` entry in v2 QueryIR carries a `path` (empty = root model),
/// and every payload carries a `joins` list (empty for now). Until #270 lands JOIN
/// rendering in the SELECT walkers, a non-empty `joins` list or any non-empty `path`
/// describes a query this build cannot lower correctly — silently ignoring it would mean
/// mis-rendered SQL (dropping a filter the caller believes is applied). Fail loud instead.
///
/// # Errors
/// Returns `Err(String)` naming the unsupported `joins`/`path` content.
fn reject_unsupported_traversal(payload: &QueryIrPayload) -> Result<(), String> {
    if !payload.joins.is_empty() {
        return Err(format!(
            "QueryIR joins are not yet supported by this build (received {} join(s)); \
             relation traversal ships in a later release",
            payload.joins.len()
        ));
    }
    for node in &payload.where_clause {
        reject_non_empty_leaf_path(node)?;
    }
    for order in &payload.order_by {
        if !order.path.is_empty() {
            return Err(format!(
                "QueryIR order_by path is not yet supported by this build (column {:?} \
                 carries path {:?}); relation traversal ships in a later release",
                order.column, order.path
            ));
        }
    }
    Ok(())
}

fn reject_non_empty_leaf_path(node: &QueryNode) -> Result<(), String> {
    match node {
        QueryNode::Leaf { column, path, .. } => {
            if !path.is_empty() {
                return Err(format!(
                    "QueryIR leaf path is not yet supported by this build (column {:?} \
                     carries path {:?}); relation traversal ships in a later release",
                    column, path
                ));
            }
            Ok(())
        }
        QueryNode::Compound { left, right, .. } => {
            reject_non_empty_leaf_path(left)?;
            reject_non_empty_leaf_path(right)
        }
    }
}

/// Many-to-many join context for filtered relation loads.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct M2mContext {
    /// Association/join table name.
    pub join_table: String,
    /// FK column pointing at the source model.
    pub source_col: String,
    /// FK column pointing at the target model.
    pub target_col: String,
    /// Source row primary key (JSON scalar).
    pub source_id: Value,
}

/// Runtime query plan: canonical IR payload plus per-query runtime state.
///
/// The runtime fields (`postgres_enum_udt`, `registration`) are NOT IR and are
/// never serialized (FF-F F-4).
#[derive(Debug, Clone)]
pub struct QueryPlan {
    /// Target model class name.
    #[allow(dead_code)]
    pub model_name: String,
    /// Root predicates (implicit AND), as canonical IR nodes.
    pub where_clause: Vec<QueryNode>,
    /// `ORDER BY` terms in application order; empty = no ORDER BY.
    pub order_by: Vec<QueryOrderBy>,
    /// `LIMIT` clause.
    pub limit: Option<u64>,
    /// `OFFSET` clause.
    pub offset: Option<u64>,
    /// M2M join filter when loading through an association table.
    pub m2m: Option<M2mContext>,
    /// Populated from `pg_catalog` before building filter SQL. Not part of the
    /// Python query IR payload.
    pub postgres_enum_udt: HashMap<String, String>,
    /// The queried model's registration (schema + codec plan), resolved once
    /// per query from `MODEL_REGISTRY` — no per-value registry locks. `None`
    /// when the model is not registered (fallback generic binds).
    pub registration: Option<std::sync::Arc<crate::state::RegisteredModel>>,
}

impl QueryPlan {
    /// Build a runtime plan from canonical query IR.
    ///
    /// # Arguments
    /// * `payload` — Deserialized [`QueryIrPayload`] from Python.
    ///
    /// # Returns
    /// A `QueryPlan` with empty `postgres_enum_udt` (callers populate enum UDT
    /// from catalog before building SQL) and the model registration resolved.
    ///
    /// # Errors
    /// Returns `Err(String)` when the `m2m` JSON blob cannot deserialize into [`M2mContext`],
    /// or when the payload carries relation-traversal shape (`joins`, or a `path` on any leaf
    /// or `order_by` entry) that this build cannot render yet (see
    /// [`reject_unsupported_traversal`]).
    pub fn from_ir_payload(payload: QueryIrPayload) -> Result<QueryPlan, String> {
        reject_unsupported_traversal(&payload)?;
        let m2m: Option<M2mContext> = match payload.m2m {
            Some(value) => serde_json::from_value(value)
                .map(Some)
                .map_err(|e| format!("invalid QueryIR m2m payload: {e}"))?,
            None => None,
        };
        let registration = crate::state::MODEL_REGISTRY
            .read()
            .ok()
            .and_then(|registry| registry.get(&payload.model_name).cloned());
        Ok(QueryPlan {
            model_name: payload.model_name,
            where_clause: payload.where_clause,
            order_by: payload.order_by,
            limit: payload.limit,
            offset: payload.offset,
            m2m,
            postgres_enum_udt: HashMap::new(),
            registration,
        })
    }

    /// Combine all root predicates into one SeaQuery `Condition` (AND of nodes).
    ///
    /// # Arguments
    /// * `backend` — Dialect for typed binds and operator lowering.
    /// * `qualify_with` — When `Some(root_table)`, every column reference is qualified
    ///   as `root_table.column` (sea_query `Expr::col((Alias::new(root_table),
    ///   Alias::new(column)))`) — required for SELECT (`fetch_filtered`/`count_filtered`)
    ///   so bare columns can't collide with a future JOINed table (#270). `None` leaves
    ///   columns unqualified, which mutating statements (`UPDATE`/`DELETE ... WHERE`) use
    ///   today (single-table target, no JOIN to disambiguate against).
    ///
    /// # Returns
    /// `Condition::all()` with each `where_clause` node applied.
    ///
    /// # Errors
    /// Returns `Err(String)` for unsupported compound operators.
    pub fn to_condition_for_backend(
        &self,
        backend: Dialect,
        qualify_with: Option<&str>,
    ) -> Result<Condition, String> {
        let mut condition = Condition::all();
        for node in &self.where_clause {
            condition =
                condition.add(self.node_to_condition_for_backend(node, backend, qualify_with)?);
        }
        Ok(condition)
    }

    fn node_to_condition_for_backend(
        &self,
        node: &QueryNode,
        backend: Dialect,
        qualify_with: Option<&str>,
    ) -> Result<Condition, String> {
        match node {
            QueryNode::Compound {
                operator,
                left,
                right,
            } => {
                let left_cond = self.node_to_condition_for_backend(left, backend, qualify_with)?;
                let right_cond =
                    self.node_to_condition_for_backend(right, backend, qualify_with)?;
                Ok(match operator.as_str() {
                    "OR" => Condition::any().add(left_cond).add(right_cond),
                    "AND" => Condition::all().add(left_cond).add(right_cond),
                    op => return Err(format!("unsupported compound QueryNode operator: {op}")),
                })
            }
            QueryNode::Leaf {
                operator,
                column,
                value,
                // Always `[]` here: `from_ir_payload` rejects a non-empty leaf `path`
                // before a `QueryPlan` ever exists (#269 defensive loudness; #270
                // will thread this into JOIN-qualified column lowering).
                path: _,
            } => {
                let col = match qualify_with {
                    Some(root_table) => Expr::col((Alias::new(root_table), Alias::new(column))),
                    None => Expr::col(Alias::new(column)),
                };
                // IR always carries a value object; JSON null arrives as
                // `QueryValue { kind: "null", value: Value::Null }`. SQL
                // `col = NULL` is never true — use `IS NULL` / `IS NOT NULL`
                // for `== None` / `!= None`.
                let rhs_is_json_null = value.value.is_null();
                let val = &value.value;
                let expr: SimpleExpr = match operator.as_str() {
                    "==" if rhs_is_json_null => col.is_null(),
                    "!=" if rhs_is_json_null => col.is_not_null(),
                    "==" => {
                        col.eq(self.value_rhs_simple_expr_for_backend(column, val, false, backend))
                    }
                    "!=" => {
                        col.ne(self.value_rhs_simple_expr_for_backend(column, val, false, backend))
                    }
                    "<" => {
                        col.lt(self.value_rhs_simple_expr_for_backend(column, val, false, backend))
                    }
                    "<=" => {
                        col.lte(self.value_rhs_simple_expr_for_backend(column, val, false, backend))
                    }
                    ">" => {
                        col.gt(self.value_rhs_simple_expr_for_backend(column, val, false, backend))
                    }
                    ">=" => {
                        col.gte(self.value_rhs_simple_expr_for_backend(column, val, false, backend))
                    }
                    "IN" => {
                        if let Some(vals) = val.as_array() {
                            let rhs: Vec<SimpleExpr> = vals
                                .iter()
                                .map(|v| {
                                    self.value_rhs_simple_expr_for_backend(
                                        column, v, false, backend,
                                    )
                                })
                                .collect();
                            col.is_in(rhs)
                        } else {
                            col.eq(self
                                .value_rhs_simple_expr_for_backend(column, val, false, backend))
                        }
                    }
                    "LIKE" => {
                        let pattern = match val {
                            Value::String(s) => s.clone(),
                            _ => val.to_string(),
                        };
                        col.like(pattern)
                    }
                    _ => {
                        col.eq(self.value_rhs_simple_expr_for_backend(column, val, false, backend))
                    }
                };
                Ok(Condition::all().add(expr))
            }
        }
    }

    /// Right-hand side expression for an UPDATE column value or a query-filter
    /// comparison.
    ///
    /// Schema-driven typed binds (per the typed-null-binds refactor):
    /// - On Postgres, UUID columns receive a typed `Value::Uuid(Some(_))`
    ///   bind (no `CAST(... AS uuid)`). Parse failures fall through to text
    ///   so Postgres still surfaces the input error.
    /// - Binary columns receive a typed `Value::Bytes(Some(_))` bind.
    /// - `Value::Null` picks a typed SeaQuery `None` variant from column
    ///   metadata so `Option::<T>::None` reaches the wire with the right OID.
    /// - Temporal types (`date`, `date-time`) and `Decimal` (`numeric`)
    ///   continue to use `CAST` -- typed binds for these are deferred (see
    ///   issue #40 for temporal; plan §3 for Decimal).
    ///
    /// `infer_uuid_without_schema` is used for M2M join filters where the RHS
    /// is a UUID string but the join column is not described on the queried
    /// model's schema.
    pub fn value_rhs_simple_expr_for_backend(
        &self,
        col_name: &str,
        val: &Value,
        infer_uuid_without_schema: bool,
        backend: Dialect,
    ) -> SimpleExpr {
        crate::codec::query_bind_expr(
            self.registration.as_ref().map(|m| &m.codec_plan),
            col_name,
            val,
            infer_uuid_without_schema,
            backend,
            &self.postgres_enum_udt,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::QueryPlan;
    use crate::state::Dialect;
    use sea_query::{Alias, PostgresQueryBuilder, Query, SqliteQueryBuilder, Value as SeaValue};
    use serde_json::json;
    use std::collections::HashMap;

    fn empty_query_plan(model_name: &str) -> QueryPlan {
        let registration = crate::state::MODEL_REGISTRY
            .read()
            .ok()
            .and_then(|registry| registry.get(model_name).cloned());
        QueryPlan {
            model_name: model_name.to_string(),
            where_clause: Vec::new(),
            order_by: Vec::new(),
            limit: None,
            offset: None,
            m2m: None,
            postgres_enum_udt: HashMap::new(),
            registration,
        }
    }

    fn extract_pg_rhs_value(rhs: sea_query::SimpleExpr) -> SeaValue {
        let (_, values) = Query::insert()
            .into_table(Alias::new("t"))
            .columns([Alias::new("c")])
            .values_panic([rhs])
            .build(PostgresQueryBuilder);
        values.0.into_iter().next().expect("one value")
    }

    #[test]
    fn query_plan_builds_from_ir_payload_and_lowers_null_eq_to_is_null() {
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(serde_json::json!({
            "model_name": "Pending",
            "where": [
                {"node_kind": "leaf", "column": "attached_at", "operator": "==",
                 "value": {"kind": "null", "value": null}, "path": []},
                {"node_kind": "compound", "operator": "OR",
                 "left": {"node_kind": "leaf", "column": "age", "operator": ">=",
                          "value": {"kind": "int", "value": 18}, "path": []},
                 "right": {"node_kind": "leaf", "column": "name", "operator": "LIKE",
                           "value": {"kind": "string", "value": "a%"}, "path": []}}
            ],
            "order_by": [{"column": "age", "direction": "desc", "path": []}],
            "limit": 10, "offset": 5, "m2m": null, "joins": []
        }))
        .expect("payload deserializes");
        let plan = super::QueryPlan::from_ir_payload(payload).expect("plan builds");
        assert_eq!(plan.order_by.len(), 1);
        assert_eq!(plan.limit, Some(10));

        let mut select = Query::select();
        select.from(Alias::new("pending")).cond_where(
            plan.to_condition_for_backend(Dialect::Sqlite, None)
                .expect("valid"),
        );
        let sql = select.to_string(SqliteQueryBuilder).to_lowercase();
        assert!(
            sql.contains("is null"),
            "IR null eq must lower to IS NULL: {sql}"
        );
        assert!(!sql.contains("= null"), "must not emit = NULL: {sql}");
        assert!(sql.contains("or"), "compound OR preserved: {sql}");
    }

    #[test]
    fn null_ne_emits_is_not_null_for_sqlite() {
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Pending",
            "where": [
                {"node_kind": "leaf", "column": "payload", "operator": "!=",
                 "value": {"kind": "null", "value": null}, "path": []}
            ],
            "order_by": [],
            "limit": null, "offset": null, "m2m": null, "joins": []
        }))
        .expect("payload deserializes");
        let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");
        let mut select = Query::select();
        select.from(Alias::new("pending")).cond_where(
            plan.to_condition_for_backend(Dialect::Sqlite, None)
                .expect("valid test query"),
        );
        let sql = select.to_string(SqliteQueryBuilder).to_lowercase();
        assert!(
            sql.contains("is not null"),
            "expected IS NOT NULL, got {sql}"
        );
    }

    #[test]
    fn to_condition_for_backend_qualifies_column_with_root_table_when_requested() {
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Pending",
            "where": [
                {"node_kind": "leaf", "column": "age", "operator": ">=",
                 "value": {"kind": "int", "value": 18}, "path": []}
            ],
            "order_by": [],
            "limit": null, "offset": null, "m2m": null, "joins": []
        }))
        .expect("payload deserializes");
        let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");

        let mut select = Query::select();
        select.from(Alias::new("pending")).cond_where(
            plan.to_condition_for_backend(Dialect::Sqlite, Some("pending"))
                .expect("valid test query"),
        );
        let sql = select.to_string(SqliteQueryBuilder).to_lowercase();
        assert!(
            sql.contains("\"pending\".\"age\"") || sql.contains("`pending`.`age`"),
            "expected the root table alias to qualify the WHERE column, got {sql}"
        );
    }

    /// Same qualification, rendered through the Postgres builder (acceptance criteria:
    /// "Rendered SQL qualifies all column references by the root alias on both backends").
    #[test]
    fn to_condition_for_backend_qualifies_column_with_root_table_on_postgres() {
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Pending",
            "where": [
                {"node_kind": "leaf", "column": "age", "operator": ">=",
                 "value": {"kind": "int", "value": 18}, "path": []}
            ],
            "order_by": [],
            "limit": null, "offset": null, "m2m": null, "joins": []
        }))
        .expect("payload deserializes");
        let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");

        let mut select = Query::select();
        select.from(Alias::new("pending")).cond_where(
            plan.to_condition_for_backend(Dialect::Postgres, Some("pending"))
                .expect("valid test query"),
        );
        let sql = select.to_string(PostgresQueryBuilder).to_lowercase();
        assert!(
            sql.contains("\"pending\".\"age\""),
            "expected the root table alias to qualify the WHERE column on Postgres, got {sql}"
        );
    }

    #[test]
    fn to_condition_for_backend_leaves_column_unqualified_without_root_table() {
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Pending",
            "where": [
                {"node_kind": "leaf", "column": "age", "operator": ">=",
                 "value": {"kind": "int", "value": 18}, "path": []}
            ],
            "order_by": [],
            "limit": null, "offset": null, "m2m": null, "joins": []
        }))
        .expect("payload deserializes");
        let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");

        let mut select = Query::select();
        select.from(Alias::new("pending")).cond_where(
            plan.to_condition_for_backend(Dialect::Sqlite, None)
                .expect("valid test query"),
        );
        let sql = select.to_string(SqliteQueryBuilder).to_lowercase();
        assert!(
            !sql.contains("\"pending\".\"age\"") && !sql.contains("`pending`.`age`"),
            "unqualified request must not qualify the WHERE column, got {sql}"
        );
    }

    #[test]
    fn from_ir_payload_rejects_non_empty_joins() {
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Pending",
            "where": [], "order_by": [], "limit": null, "offset": null, "m2m": null,
            "joins": [{"join_type": "inner", "path": [
                {"relation": "account", "from_column": "account_id",
                 "to_table": "account", "to_column": "id"}
            ]}]
        }))
        .expect("payload deserializes");

        let err = QueryPlan::from_ir_payload(payload)
            .expect_err("non-empty joins must be rejected until #270 renders them");
        assert!(
            err.contains("join"),
            "error should mention joins are unsupported: {err}"
        );
    }

    #[test]
    fn from_ir_payload_rejects_non_empty_leaf_path() {
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Pending",
            "where": [
                {"node_kind": "leaf", "column": "name", "operator": "==",
                 "value": {"kind": "string", "value": "a"}, "path": ["account"]}
            ],
            "order_by": [], "limit": null, "offset": null, "m2m": null, "joins": []
        }))
        .expect("payload deserializes");

        let err = QueryPlan::from_ir_payload(payload)
            .expect_err("a non-empty leaf path must be rejected until #270 renders joins");
        assert!(
            err.contains("path"),
            "error should mention the unsupported path: {err}"
        );
    }

    #[test]
    fn from_ir_payload_rejects_non_empty_leaf_path_nested_in_compound() {
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Pending",
            "where": [
                {"node_kind": "compound", "operator": "AND",
                 "left": {"node_kind": "leaf", "column": "active", "operator": "==",
                          "value": {"kind": "bool", "value": true}, "path": []},
                 "right": {"node_kind": "leaf", "column": "name", "operator": "==",
                           "value": {"kind": "string", "value": "a"}, "path": ["account"]}}
            ],
            "order_by": [], "limit": null, "offset": null, "m2m": null, "joins": []
        }))
        .expect("payload deserializes");

        let err = QueryPlan::from_ir_payload(payload)
            .expect_err("a non-empty path nested in a compound node must be rejected");
        assert!(
            err.contains("path"),
            "error should mention the unsupported path: {err}"
        );
    }

    #[test]
    fn from_ir_payload_rejects_non_empty_order_by_path() {
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Pending",
            "where": [], "limit": null, "offset": null, "m2m": null, "joins": [],
            "order_by": [{"column": "name", "direction": "asc", "path": ["account"]}]
        }))
        .expect("payload deserializes");

        let err = QueryPlan::from_ir_payload(payload)
            .expect_err("a non-empty order_by path must be rejected until #270 renders joins");
        assert!(
            err.contains("path"),
            "error should mention the unsupported path: {err}"
        );
    }

    #[test]
    fn uuid_rhs_emits_typed_uuid_bind_on_postgres_no_cast() {
        let plan = empty_query_plan("Widget");
        let uuid_str = "3f4c4ca7-a7e7-40d6-8d83-8f4ddf3285e6";

        let postgres_rhs = plan.value_rhs_simple_expr_for_backend(
            "widget_id",
            &json!(uuid_str),
            true,
            Dialect::Postgres,
        );
        let postgres_sql = Query::select()
            .expr(postgres_rhs.clone())
            .to_string(PostgresQueryBuilder);

        assert!(
            !postgres_sql.contains("AS uuid"),
            "Postgres UUID rhs should no longer use CAST: {postgres_sql}"
        );
        match extract_pg_rhs_value(postgres_rhs) {
            SeaValue::Uuid(Some(u)) => assert_eq!(u.to_string(), uuid_str),
            other => panic!("expected typed Uuid bind, got {other:?}"),
        }
    }

    #[test]
    fn uuid_rhs_passes_through_as_text_on_sqlite() {
        let plan = empty_query_plan("Widget");
        let uuid_str = "3f4c4ca7-a7e7-40d6-8d83-8f4ddf3285e6";

        let sqlite_rhs = plan.value_rhs_simple_expr_for_backend(
            "widget_id",
            &json!(uuid_str),
            true,
            Dialect::Sqlite,
        );
        let sqlite_sql = Query::select()
            .expr(sqlite_rhs)
            .to_string(SqliteQueryBuilder);

        assert!(
            !sqlite_sql.contains("AS uuid"),
            "SQLite must never CAST: {sqlite_sql}"
        );
    }

    #[test]
    fn null_rhs_emits_typed_int_null_for_int_column() {
        // Schema-driven column type info -- we register a Widget with a
        // nullable integer column "count" so model_column lookups succeed.
        crate::state::MODEL_REGISTRY.write().unwrap().insert(
            "WidgetIntNull".to_string(),
            crate::state::RegisteredModel::new_for_test(json!({
                "properties": {
                    "count": {"anyOf": [{"type": "integer"}, {"type": "null"}]}
                }
            }), "widget".to_string()),
        );
        let plan = empty_query_plan("WidgetIntNull");

        let rhs = plan.value_rhs_simple_expr_for_backend(
            "count",
            &serde_json::Value::Null,
            false,
            Dialect::Postgres,
        );

        match extract_pg_rhs_value(rhs) {
            SeaValue::BigInt(None) => {}
            other => panic!("expected BigInt(None), got {other:?}"),
        }
    }

    #[test]
    fn null_rhs_emits_typed_bool_null_for_bool_column() {
        crate::state::MODEL_REGISTRY.write().unwrap().insert(
            "WidgetBoolNull".to_string(),
            crate::state::RegisteredModel::new_for_test(json!({
                "properties": {
                    "active": {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
                }
            }), "widget".to_string()),
        );
        let plan = empty_query_plan("WidgetBoolNull");

        let rhs = plan.value_rhs_simple_expr_for_backend(
            "active",
            &serde_json::Value::Null,
            false,
            Dialect::Postgres,
        );

        match extract_pg_rhs_value(rhs) {
            SeaValue::Bool(None) => {}
            other => panic!("expected Bool(None), got {other:?}"),
        }
    }

    #[test]
    fn null_rhs_emits_typed_uuid_null_for_uuid_column() {
        crate::state::MODEL_REGISTRY.write().unwrap().insert(
            "WidgetUuidNull".to_string(),
            crate::state::RegisteredModel::new_for_test(json!({
                "properties": {
                    "id": {"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]}
                }
            }), "widget".to_string()),
        );
        let plan = empty_query_plan("WidgetUuidNull");

        let rhs = plan.value_rhs_simple_expr_for_backend(
            "id",
            &serde_json::Value::Null,
            false,
            Dialect::Postgres,
        );

        match extract_pg_rhs_value(rhs) {
            SeaValue::Uuid(None) => {}
            other => panic!("expected Uuid(None), got {other:?}"),
        }
    }

    #[test]
    fn binary_rhs_emits_typed_bytes_no_cast() {
        crate::state::MODEL_REGISTRY.write().unwrap().insert(
            "WidgetBinary".to_string(),
            crate::state::RegisteredModel::new_for_test(json!({
                "properties": {
                    "blob": {"type": "string", "format": "binary"}
                }
            }), "widget".to_string()),
        );
        let plan = empty_query_plan("WidgetBinary");

        let rhs = plan.value_rhs_simple_expr_for_backend(
            "blob",
            &json!("some-bytes"),
            false,
            Dialect::Postgres,
        );
        let sql = Query::select()
            .expr(rhs.clone())
            .to_string(PostgresQueryBuilder);

        assert!(
            !sql.contains("AS bytea"),
            "binary rhs should no longer CAST: {sql}"
        );
        match extract_pg_rhs_value(rhs) {
            SeaValue::Bytes(Some(b)) => assert_eq!(*b, b"some-bytes".to_vec()),
            other => panic!("expected typed Bytes bind, got {other:?}"),
        }
    }

    #[test]
    fn enum_rhs_emits_cast_to_schema_enum_type_on_postgres() {
        let mut plan = empty_query_plan("WidgetColor");
        plan.postgres_enum_udt
            .insert("color".to_string(), "color".to_string());

        let rhs = plan.value_rhs_simple_expr_for_backend(
            "color",
            &json!("red"),
            false,
            Dialect::Postgres,
        );
        let sql = Query::select().expr(rhs).to_string(PostgresQueryBuilder);

        assert!(
            sql.to_lowercase().contains("as \"color\"") || sql.to_lowercase().contains("as color"),
            "enum filter rhs should CAST to the UDT name, got: {sql}"
        );
    }

    #[test]
    fn enum_rhs_skips_cast_without_native_enum_column() {
        crate::state::MODEL_REGISTRY.write().unwrap().insert(
            "WidgetTextColor".to_string(),
            crate::state::RegisteredModel::new_for_test(json!({
                "properties": {
                    "color": {"enum_type_name": "color", "db_type": "text"}
                }
            }), "widget".to_string()),
        );
        let plan = empty_query_plan("WidgetTextColor");

        let rhs = plan.value_rhs_simple_expr_for_backend(
            "color",
            &json!("red"),
            false,
            Dialect::Postgres,
        );
        let sql = Query::select().expr(rhs).to_string(PostgresQueryBuilder);

        assert!(
            !sql.to_lowercase().contains("as \"color\"")
                && !sql.to_lowercase().contains("as color"),
            "auto-migrate TEXT enum columns must not cast without catalog UDT: {sql}"
        );
    }

    #[test]
    fn decimal_rhs_keeps_numeric_cast_for_now() {
        // Native numeric typed binds are deferred (plan §3 Scope Boundaries);
        // Decimal still uses CAST AS numeric on Postgres.
        crate::state::MODEL_REGISTRY.write().unwrap().insert(
            "WidgetDecimal".to_string(),
            crate::state::RegisteredModel::new_for_test(json!({
                "properties": {
                    "amount": {
                        // The enriched shape registration emits for Decimal
                        // annotations; the pattern alone must NOT make a
                        // column decimal (F5).
                        "anyOf": [
                            {"type": "number"},
                            {"type": "string", "pattern": "^-?\\d+(\\.\\d+)?$"}
                        ],
                        "format": "decimal"
                    }
                }
            }), "widget".to_string()),
        );
        let plan = empty_query_plan("WidgetDecimal");

        let rhs = plan.value_rhs_simple_expr_for_backend(
            "amount",
            &json!("12.34"),
            false,
            Dialect::Postgres,
        );
        let sql = Query::select().expr(rhs).to_string(PostgresQueryBuilder);

        assert!(
            sql.contains("AS numeric"),
            "decimal cast preserved until follow-up: {sql}"
        );
    }
}
