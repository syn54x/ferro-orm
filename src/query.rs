//! Query IR lowering to SeaQuery `Condition`s.
//!
//! Consumes canonical [`QueryIrPayload`] envelopes from `ferro_schema_ir` — the only
//! query shape in Rust (FF-F F-4) — and builds backend-aware filter SQL with typed
//! null/UUID/enum binds via [`crate::codec`].

use crate::state::Dialect;
use ferro_schema_ir::{QueryIrPayload, QueryJoin, QueryNode, QueryOrderBy};
use sea_query::{Alias, Condition, Expr, SimpleExpr};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{HashMap, HashSet};

/// Reject any relation-traversal shape on a WHERE tree (used by mutating walkers).
///
/// UPDATE/DELETE with relation joins would need multi-table mutation semantics no
/// backend portably supports, so the Python builder never emits `joins` on a
/// mutating payload. A leaf carrying a relation `path` here (or a non-empty `joins`
/// list, checked by the caller) can only be misuse — fail loud rather than silently
/// drop the traversal filter.
fn reject_non_empty_leaf_path(node: &QueryNode) -> Result<(), String> {
    match node {
        QueryNode::Leaf { column, path, .. } => {
            if !path.is_empty() {
                return Err(format!(
                    "WHERE column {:?} carries relation path {:?}",
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

/// How to qualify a WHERE column reference when lowering to SeaQuery.
///
/// SELECT walkers use [`ColumnQualifier::Joined`] so a root-model column gets the
/// root table alias and a relation-path leaf gets its JOIN alias; mutating walkers
/// use [`ColumnQualifier::RootTable`] (single-table target, paths rejected upstream);
/// unit tests use [`ColumnQualifier::Unqualified`].
enum ColumnQualifier<'a> {
    /// Leave columns unqualified (bare `column`).
    Unqualified,
    /// Qualify every column with `root_table.column` (all paths must be empty).
    RootTable(&'a str),
    /// Qualify by JOIN alias: empty path -> `root_table`, else the path's alias.
    Joined {
        root_table: &'a str,
        prefix_alias: &'a HashMap<Vec<String>, String>,
    },
}

/// Qualify a column reference (a WHERE leaf or an `ORDER BY` term) by its
/// relation path's JOIN alias (SELECT walkers, #270/#271): an empty `path`
/// qualifies by `root_table`, a non-empty `path` resolves through `join_plan`'s
/// prefix map. Shared by WHERE lowering and the `ORDER BY` render loop so both
/// clauses qualify identically against the same join plan.
///
/// # Errors
/// Returns `Err(String)` when `path` has no matching JOIN entry — never
/// silently unqualified (the Python builder always registers the join for a
/// path-carrying `order_by` entry, same as `where()`; this is defense-in-depth).
pub fn qualify_column_with_joins(
    root_table: &str,
    join_plan: &JoinPlan,
    column: &str,
    path: &[String],
) -> Result<Expr, String> {
    let qualifier = ColumnQualifier::Joined {
        root_table,
        prefix_alias: &join_plan.prefix_alias,
    };
    qualify_leaf_column(&qualifier, column, path)
}

/// One rendered relation JOIN edge: `<join> <to_table> AS <alias> ON
/// <prev_alias>.<from_column> = <alias>.<to_column>`.
#[derive(Debug, Clone)]
pub struct JoinRender {
    /// Validated join type token (`"inner"` or `"left"`).
    pub join_type: String,
    /// Table this edge joins into.
    pub to_table: String,
    /// Deterministic internal alias for `to_table` on this edge.
    pub alias: String,
    /// Alias of the previous hop (root table for the first hop).
    pub prev_alias: String,
    /// Local column on the previous side of the edge.
    pub from_column: String,
    /// Column on `to_table` the edge joins against.
    pub to_column: String,
}

/// Resolved JOIN plan for one query: ordered render steps plus a full-path ->
/// alias map for qualifying WHERE columns (#270).
#[derive(Debug, Clone, Default)]
pub struct JoinPlan {
    /// JOIN edges in first-appearance order (shared prefixes deduped to one edge).
    pub renders: Vec<JoinRender>,
    /// Full relation path -> alias, for qualifying path-carrying WHERE leaves
    /// and `ORDER BY` terms.
    pub prefix_alias: HashMap<Vec<String>, String>,
}

/// Validate a join_type token from the IR (`"inner"`/`"left"`), erroring loudly
/// on anything else so an unknown type can never silently mis-render.
fn validate_join_type(join_type: &str) -> Result<&'static str, String> {
    match join_type {
        "inner" => Ok("inner"),
        "left" => Ok("left"),
        other => Err(format!(
            "unsupported QueryIR join_type {other:?}; expected \"inner\" or \"left\""
        )),
    }
}

/// Build the qualified column reference for a WHERE leaf under `qualifier`.
///
/// # Errors
/// Returns `Err(String)` when a non-`Joined` qualifier meets a path-carrying leaf
/// (paths are rejected upstream for those statements), or when a `Joined` leaf's
/// relation `path` has no assigned JOIN alias — never silently unqualified.
fn qualify_leaf_column(
    qualifier: &ColumnQualifier<'_>,
    column: &str,
    path: &[String],
) -> Result<Expr, String> {
    match qualifier {
        ColumnQualifier::Unqualified => {
            if !path.is_empty() {
                return Err(format!(
                    "WHERE column {column:?} carries relation path {path:?} but this \
                     statement renders no joins"
                ));
            }
            Ok(Expr::col(Alias::new(column)))
        }
        ColumnQualifier::RootTable(root_table) => {
            if !path.is_empty() {
                return Err(format!(
                    "WHERE column {column:?} carries relation path {path:?} but this \
                     statement renders no joins"
                ));
            }
            Ok(Expr::col((Alias::new(*root_table), Alias::new(column))))
        }
        ColumnQualifier::Joined {
            root_table,
            prefix_alias,
        } => {
            if path.is_empty() {
                return Ok(Expr::col((Alias::new(*root_table), Alias::new(column))));
            }
            let alias = prefix_alias.get(path).ok_or_else(|| {
                format!(
                    "column {column:?} carries relation path {path:?} with no \
                     matching join entry"
                )
            })?;
            Ok(Expr::col((Alias::new(alias.as_str()), Alias::new(column))))
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
    /// Relation JOINs collected from WHERE traversal, in registration order.
    /// Empty for non-traversal queries; rendered by the SELECT walkers (#270).
    pub joins: Vec<QueryJoin>,
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
    /// Returns `Err(String)` when the `m2m` JSON blob cannot deserialize into
    /// [`M2mContext`]. WHERE `path`s, `order_by` `path`s, and the `joins` list are all
    /// accepted here and rendered by the SELECT walkers (#270 WHERE traversal, #271
    /// `order_by` traversal); mutating walkers reject WHERE traversal separately via
    /// [`QueryPlan::ensure_no_traversal`] (the Python builder never emits a path-carrying
    /// `order_by` entry on a mutating payload, so no equivalent guard is needed there).
    pub fn from_ir_payload(payload: QueryIrPayload) -> Result<QueryPlan, String> {
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
            joins: payload.joins,
            m2m,
            postgres_enum_udt: HashMap::new(),
            registration,
        })
    }

    /// Reject any relation traversal on this plan (mutating-walker guard, #270).
    ///
    /// # Errors
    /// Returns `Err(String)` if the plan carries `joins` or any WHERE leaf with a
    /// non-empty relation `path`. UPDATE/DELETE render a single-table statement, so
    /// traversal here is misuse; the error detail names the offending shape.
    pub fn ensure_no_traversal(&self) -> Result<(), String> {
        if !self.joins.is_empty() {
            return Err(format!("query carries {} relation join(s)", self.joins.len()));
        }
        for node in &self.where_clause {
            reject_non_empty_leaf_path(node)?;
        }
        Ok(())
    }

    /// Assign deterministic aliases and build the ordered JOIN render plan (#270,
    /// with per-edge LEFT/INNER resolution #272).
    ///
    /// Walks each `joins` entry's hops in order, keeping a `prefix -> alias` map so
    /// the first-unseen prefix emits exactly one edge and shared prefixes across
    /// entries (e.g. `[account, owner]` and `[account]`) dedup to one JOIN each.
    /// Aliases are `j{i}_{relation}` (`i` = 1-based order of first appearance,
    /// `relation` = the prefix's last hop); a collision with `root_table`, a
    /// `reserved` name (the M2M association table), or an already-assigned alias
    /// appends `_x` until unique.
    ///
    /// Each rendered edge's join type is resolved at the EDGE level, not per
    /// entry: an edge is `LEFT` iff some `"left"` entry covers it, else `INNER`
    /// (ADR-0006 whole-path rule — `.left_join` marks its whole path, so every
    /// prefix of a `"left"` entry is a LEFT edge). Resolving before render keeps
    /// the rendered type independent of entry order and of which entry first
    /// materializes a shared prefix; e.g. a `"left"` `[account]` entry and an
    /// `"inner"` `[account, owner]` entry render the `account` edge LEFT and the
    /// `owner` edge INNER regardless of their order on the wire (#272).
    ///
    /// # Errors
    /// Returns `Err(String)` for an unknown `join_type` (see [`validate_join_type`]).
    pub fn build_join_plan(
        &self,
        root_table: &str,
        reserved: &[&str],
    ) -> Result<JoinPlan, String> {
        // Validate every entry's join_type and collect the set of LEFT edges.
        // A `"left"` entry is whole-path, so every one of its prefixes is a LEFT
        // edge; an edge is LEFT iff any `"left"` entry contains it.
        let mut left_edges: HashSet<Vec<String>> = HashSet::new();
        for join in &self.joins {
            let join_type = validate_join_type(&join.join_type)?;
            if join_type == "left" {
                let mut prefix: Vec<String> = Vec::new();
                for hop in &join.path {
                    prefix.push(hop.relation.clone());
                    left_edges.insert(prefix.clone());
                }
            }
        }

        let mut prefix_alias: HashMap<Vec<String>, String> = HashMap::new();
        let mut used: HashSet<String> = HashSet::new();
        used.insert(root_table.to_string());
        for name in reserved {
            used.insert((*name).to_string());
        }
        let mut renders: Vec<JoinRender> = Vec::new();
        let mut next_index = 1usize;
        for join in &self.joins {
            let mut prefix: Vec<String> = Vec::new();
            let mut prev_alias = root_table.to_string();
            for hop in &join.path {
                prefix.push(hop.relation.clone());
                if let Some(existing) = prefix_alias.get(&prefix) {
                    prev_alias = existing.clone();
                    continue;
                }
                let edge_join_type = if left_edges.contains(&prefix) {
                    "left"
                } else {
                    "inner"
                };
                let mut alias = format!("j{}_{}", next_index, hop.relation);
                next_index += 1;
                while used.contains(&alias) {
                    alias.push_str("_x");
                }
                used.insert(alias.clone());
                prefix_alias.insert(prefix.clone(), alias.clone());
                renders.push(JoinRender {
                    join_type: edge_join_type.to_string(),
                    to_table: hop.to_table.clone(),
                    alias: alias.clone(),
                    prev_alias: prev_alias.clone(),
                    from_column: hop.from_column.clone(),
                    to_column: hop.to_column.clone(),
                });
                prev_alias = alias;
            }
        }
        Ok(JoinPlan {
            renders,
            prefix_alias,
        })
    }

    /// Combine all root predicates into one SeaQuery `Condition` (AND of nodes).
    ///
    /// # Arguments
    /// * `backend` — Dialect for typed binds and operator lowering.
    /// * `qualify_with` — When `Some(root_table)`, every column reference is qualified
    ///   as `root_table.column` (sea_query `Expr::col((Alias::new(root_table),
    ///   Alias::new(column)))`). All four filtered walkers qualify with the root alias
    ///   in production — SELECT (`fetch_filtered`/`count_filtered`) to disambiguate
    ///   against JOINed tables (#270), and `UPDATE`/`DELETE ... WHERE` for uniformity
    ///   (both dialects accept `table.column` on a single-table mutation). `None`
    ///   leaves columns unqualified and is exercised only by unit tests; production
    ///   SELECTs that carry joins use [`QueryPlan::to_condition_with_joins`] instead.
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
        let qualifier = match qualify_with {
            Some(root_table) => ColumnQualifier::RootTable(root_table),
            None => ColumnQualifier::Unqualified,
        };
        self.build_condition(backend, &qualifier)
    }

    /// Combine root predicates into a `Condition`, qualifying each column by its
    /// relation path against `join_plan` (SELECT walkers, #270): a root-model
    /// column (empty path) gets `root_table`, a path-carrying leaf gets its JOIN
    /// alias.
    ///
    /// # Errors
    /// Returns `Err(String)` for unsupported compound operators, or a leaf whose
    /// relation `path` has no matching JOIN entry (never silently unqualified).
    pub fn to_condition_with_joins(
        &self,
        backend: Dialect,
        root_table: &str,
        join_plan: &JoinPlan,
    ) -> Result<Condition, String> {
        let qualifier = ColumnQualifier::Joined {
            root_table,
            prefix_alias: &join_plan.prefix_alias,
        };
        self.build_condition(backend, &qualifier)
    }

    fn build_condition(
        &self,
        backend: Dialect,
        qualifier: &ColumnQualifier<'_>,
    ) -> Result<Condition, String> {
        let mut condition = Condition::all();
        for node in &self.where_clause {
            condition = condition.add(self.node_to_condition_for_backend(node, backend, qualifier)?);
        }
        Ok(condition)
    }

    fn node_to_condition_for_backend(
        &self,
        node: &QueryNode,
        backend: Dialect,
        qualifier: &ColumnQualifier<'_>,
    ) -> Result<Condition, String> {
        match node {
            QueryNode::Compound {
                operator,
                left,
                right,
            } => {
                let left_cond = self.node_to_condition_for_backend(left, backend, qualifier)?;
                let right_cond = self.node_to_condition_for_backend(right, backend, qualifier)?;
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
                path,
            } => {
                let col = qualify_leaf_column(qualifier, column, path)?;
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
            joins: Vec::new(),
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

    /// A multi-hop `joins` payload plus a path-carrying leaf now builds a plan and
    /// renders INNER JOINs (#270 replaces the slice-2 blanket rejection). Pins the
    /// alias scheme, prefix dedup, and path-qualified WHERE column in one SQL string.
    fn traversal_payload() -> ferro_schema_ir::QueryIrPayload {
        serde_json::from_value(json!({
            "model_name": "Transaction",
            "where": [
                {"node_kind": "compound", "operator": "AND",
                 "left": {"node_kind": "leaf", "column": "email", "operator": "==",
                          "value": {"kind": "string", "value": "a@b.com"},
                          "path": ["account", "owner"]},
                 "right": {"node_kind": "leaf", "column": "amount", "operator": ">=",
                           "value": {"kind": "int", "value": 100}, "path": []}}
            ],
            "order_by": [{"column": "id", "direction": "asc", "path": []}],
            "limit": 50, "offset": 0, "m2m": null,
            "joins": [
                {"join_type": "inner", "path": [
                    {"relation": "account", "from_column": "account_id",
                     "to_table": "account", "to_column": "id"}
                ]},
                {"join_type": "inner", "path": [
                    {"relation": "account", "from_column": "account_id",
                     "to_table": "account", "to_column": "id"},
                    {"relation": "owner", "from_column": "owner_id",
                     "to_table": "owner", "to_column": "id"}
                ]}
            ]
        }))
        .expect("payload deserializes")
    }

    #[test]
    fn build_join_plan_assigns_aliases_and_dedups_shared_prefix() {
        let plan = QueryPlan::from_ir_payload(traversal_payload()).expect("plan builds");
        let join_plan = plan.build_join_plan("transaction", &[]).expect("join plan");

        // A 1-hop path and a 2-hop path sharing its prefix produce exactly two edges.
        assert_eq!(join_plan.renders.len(), 2, "shared prefix must dedup to 2 JOINs");
        assert_eq!(join_plan.renders[0].alias, "j1_account");
        assert_eq!(join_plan.renders[0].prev_alias, "transaction");
        assert_eq!(join_plan.renders[1].alias, "j2_owner");
        assert_eq!(join_plan.renders[1].prev_alias, "j1_account");
        assert_eq!(
            join_plan.prefix_alias.get(&vec!["account".to_string()]),
            Some(&"j1_account".to_string())
        );
        assert_eq!(
            join_plan
                .prefix_alias
                .get(&vec!["account".to_string(), "owner".to_string()]),
            Some(&"j2_owner".to_string())
        );
    }

    #[test]
    fn traversal_where_qualifies_leaf_with_join_alias() {
        let plan = QueryPlan::from_ir_payload(traversal_payload()).expect("plan builds");
        let join_plan = plan.build_join_plan("transaction", &[]).expect("join plan");
        let sql = plan
            .to_condition_with_joins(Dialect::Postgres, "transaction", &join_plan)
            .and_then(|cond| {
                let mut select = Query::select();
                select.from(Alias::new("transaction")).cond_where(cond);
                Ok(select.to_string(PostgresQueryBuilder).to_lowercase())
            })
            .expect("condition builds");
        // Path leaf qualified by its hop alias; root leaf by the root table.
        assert!(sql.contains("\"j2_owner\".\"email\""), "got {sql}");
        assert!(sql.contains("\"transaction\".\"amount\""), "got {sql}");
    }

    #[test]
    fn build_join_plan_two_paths_to_one_table_get_distinct_aliases() {
        // Transfer(from_account, to_account) -> two FKs into the same table.
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Transfer",
            "where": [], "order_by": [], "limit": null, "offset": null, "m2m": null,
            "joins": [
                {"join_type": "inner", "path": [
                    {"relation": "from_account", "from_column": "from_account_id",
                     "to_table": "account", "to_column": "id"}
                ]},
                {"join_type": "inner", "path": [
                    {"relation": "to_account", "from_column": "to_account_id",
                     "to_table": "account", "to_column": "id"}
                ]}
            ]
        }))
        .expect("payload deserializes");
        let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");
        let join_plan = plan.build_join_plan("transfer", &[]).expect("join plan");
        assert_eq!(join_plan.renders.len(), 2);
        assert_eq!(join_plan.renders[0].alias, "j1_from_account");
        assert_eq!(join_plan.renders[1].alias, "j2_to_account");
        assert_ne!(join_plan.renders[0].alias, join_plan.renders[1].alias);
        // Both edges target the same physical table under distinct aliases.
        assert_eq!(join_plan.renders[0].to_table, "account");
        assert_eq!(join_plan.renders[1].to_table, "account");
    }

    #[test]
    fn build_join_plan_uniquifies_alias_colliding_with_reserved_name() {
        // A join into a table whose alias would collide with the M2M association
        // table name gets `_x` appended until unique.
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Thing",
            "where": [], "order_by": [], "limit": null, "offset": null, "m2m": null,
            "joins": [
                {"join_type": "inner", "path": [
                    {"relation": "account", "from_column": "account_id",
                     "to_table": "account", "to_column": "id"}
                ]}
            ]
        }))
        .expect("payload deserializes");
        let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");
        let join_plan = plan
            .build_join_plan("thing", &["j1_account"])
            .expect("join plan");
        assert_eq!(join_plan.renders[0].alias, "j1_account_x");
    }

    #[test]
    fn build_join_plan_rejects_unknown_join_type() {
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Thing",
            "where": [], "order_by": [], "limit": null, "offset": null, "m2m": null,
            "joins": [
                {"join_type": "cross", "path": [
                    {"relation": "account", "from_column": "account_id",
                     "to_table": "account", "to_column": "id"}
                ]}
            ]
        }))
        .expect("payload deserializes");
        let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");
        let err = plan
            .build_join_plan("thing", &[])
            .expect_err("unknown join_type must be rejected");
        assert!(err.contains("join_type"), "got {err}");
    }

    #[test]
    fn build_join_plan_marks_single_left_edge() {
        // A lone `"left"` 1-hop entry renders its edge LEFT (#272).
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Note",
            "where": [], "order_by": [], "limit": null, "offset": null, "m2m": null,
            "joins": [
                {"join_type": "left", "path": [
                    {"relation": "account", "from_column": "account_id",
                     "to_table": "account", "to_column": "id"}
                ]}
            ]
        }))
        .expect("payload deserializes");
        let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");
        let join_plan = plan.build_join_plan("note", &[]).expect("join plan");
        assert_eq!(join_plan.renders.len(), 1);
        assert_eq!(join_plan.renders[0].join_type, "left");
        assert_eq!(join_plan.renders[0].alias, "j1_account");
    }

    #[test]
    fn build_join_plan_marks_whole_path_left_at_every_hop() {
        // Whole-path rule: a `"left"` 2-hop entry marks BOTH edges LEFT (#272).
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Transaction",
            "where": [], "order_by": [], "limit": null, "offset": null, "m2m": null,
            "joins": [
                {"join_type": "left", "path": [
                    {"relation": "account", "from_column": "account_id",
                     "to_table": "account", "to_column": "id"},
                    {"relation": "owner", "from_column": "owner_id",
                     "to_table": "owner", "to_column": "id"}
                ]}
            ]
        }))
        .expect("payload deserializes");
        let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");
        let join_plan = plan.build_join_plan("transaction", &[]).expect("join plan");
        assert_eq!(join_plan.renders.len(), 2);
        assert_eq!(join_plan.renders[0].join_type, "left", "hop-1 edge LEFT");
        assert_eq!(join_plan.renders[1].join_type, "left", "hop-2 edge LEFT");
    }

    #[test]
    fn build_join_plan_mixed_left_prefix_inner_suffix_is_order_independent() {
        // The pinned mixed case (#272): a `"left"` `[account]` entry shares its
        // prefix with an `"inner"` `[account, owner]` entry. Edge-level
        // resolution yields the account edge LEFT and the owner edge INNER — and
        // that verdict must not depend on entry order on the wire.
        let left_first = json!([
            {"join_type": "left", "path": [
                {"relation": "account", "from_column": "account_id",
                 "to_table": "account", "to_column": "id"}
            ]},
            {"join_type": "inner", "path": [
                {"relation": "account", "from_column": "account_id",
                 "to_table": "account", "to_column": "id"},
                {"relation": "owner", "from_column": "owner_id",
                 "to_table": "owner", "to_column": "id"}
            ]}
        ]);
        let inner_first = json!([
            {"join_type": "inner", "path": [
                {"relation": "account", "from_column": "account_id",
                 "to_table": "account", "to_column": "id"},
                {"relation": "owner", "from_column": "owner_id",
                 "to_table": "owner", "to_column": "id"}
            ]},
            {"join_type": "left", "path": [
                {"relation": "account", "from_column": "account_id",
                 "to_table": "account", "to_column": "id"}
            ]}
        ]);
        for joins in [left_first, inner_first] {
            let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
                "model_name": "Transaction",
                "where": [], "order_by": [], "limit": null, "offset": null, "m2m": null,
                "joins": joins
            }))
            .expect("payload deserializes");
            let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");
            let join_plan = plan.build_join_plan("transaction", &[]).expect("join plan");
            // Two edges total (shared account prefix dedups).
            assert_eq!(
                join_plan.renders.len(),
                2,
                "shared prefix dedups to 2 edges"
            );
            let account = join_plan
                .renders
                .iter()
                .find(|r| r.alias == "j1_account" || r.to_table == "account")
                .expect("account edge rendered");
            let owner = join_plan
                .renders
                .iter()
                .find(|r| r.to_table == "owner")
                .expect("owner edge rendered");
            assert_eq!(account.join_type, "left", "account edge is LEFT");
            assert_eq!(owner.join_type, "inner", "owner edge is INNER");
        }
    }

    #[test]
    fn to_condition_with_joins_errors_on_leaf_path_without_join_entry() {
        // A path-carrying leaf whose path was never registered as a join entry is a
        // loud error, never a silently unqualified column.
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Transaction",
            "where": [
                {"node_kind": "leaf", "column": "email", "operator": "==",
                 "value": {"kind": "string", "value": "a"}, "path": ["account"]}
            ],
            "order_by": [], "limit": null, "offset": null, "m2m": null, "joins": []
        }))
        .expect("payload deserializes");
        let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");
        let join_plan = plan.build_join_plan("transaction", &[]).expect("join plan");
        let err = plan
            .to_condition_with_joins(Dialect::Sqlite, "transaction", &join_plan)
            .expect_err("leaf path with no join entry must error");
        assert!(err.contains("no"), "got {err}");
    }

    #[test]
    fn ensure_no_traversal_rejects_joins_and_leaf_paths() {
        let plan = QueryPlan::from_ir_payload(traversal_payload()).expect("plan builds");
        let err = plan
            .ensure_no_traversal()
            .expect_err("mutation guard must reject joins");
        assert!(err.contains("join"), "got {err}");

        // Leaf path with no joins list is also rejected (mutating payloads set joins=[]).
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Transaction",
            "where": [
                {"node_kind": "leaf", "column": "email", "operator": "==",
                 "value": {"kind": "string", "value": "a"}, "path": ["account"]}
            ],
            "order_by": [], "limit": null, "offset": null, "m2m": null, "joins": []
        }))
        .expect("payload deserializes");
        let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");
        let err = plan
            .ensure_no_traversal()
            .expect_err("mutation guard must reject leaf paths");
        assert!(err.contains("path"), "got {err}");
    }

    #[test]
    fn from_ir_payload_accepts_non_empty_order_by_path() {
        // #271 lifts the blanket order_by-path rejection: a path-carrying order_by
        // entry now builds a plan like any other (the SELECT walkers render it).
        let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(json!({
            "model_name": "Pending",
            "where": [], "limit": null, "offset": null, "m2m": null, "joins": [
                {"join_type": "inner", "path": [
                    {"relation": "account", "from_column": "account_id",
                     "to_table": "account", "to_column": "id"}
                ]}
            ],
            "order_by": [{"column": "name", "direction": "asc", "path": ["account"]}]
        }))
        .expect("payload deserializes");

        let plan = QueryPlan::from_ir_payload(payload).expect("plan builds");
        assert_eq!(plan.order_by[0].path, vec!["account".to_string()]);
    }

    /// `qualify_column_with_joins` is the render-time seam shared by WHERE and
    /// `ORDER BY` (#271): an empty path qualifies by the root table, a registered
    /// path qualifies by its JOIN alias, and an unregistered path is a loud error.
    #[test]
    fn qualify_column_with_joins_qualifies_order_by_path_by_join_alias() {
        let plan = QueryPlan::from_ir_payload(traversal_payload()).expect("plan builds");
        let join_plan = plan.build_join_plan("transaction", &[]).expect("join plan");

        let root_col = super::qualify_column_with_joins(
            "transaction",
            &join_plan,
            "id",
            &[],
        )
        .expect("root column qualifies");
        let sql = Query::select()
            .expr(root_col)
            .to_string(PostgresQueryBuilder)
            .to_lowercase();
        assert!(sql.contains("\"transaction\".\"id\""), "got {sql}");

        let path_col = super::qualify_column_with_joins(
            "transaction",
            &join_plan,
            "email",
            &["account".to_string(), "owner".to_string()],
        )
        .expect("path column qualifies by its join alias");
        let sql = Query::select()
            .expr(path_col)
            .to_string(PostgresQueryBuilder)
            .to_lowercase();
        assert!(sql.contains("\"j2_owner\".\"email\""), "got {sql}");
    }

    #[test]
    fn qualify_column_with_joins_errors_on_order_by_path_without_join_entry() {
        let plan = QueryPlan::from_ir_payload(traversal_payload()).expect("plan builds");
        let join_plan = plan.build_join_plan("transaction", &[]).expect("join plan");

        let err = super::qualify_column_with_joins(
            "transaction",
            &join_plan,
            "name",
            &["unregistered".to_string()],
        )
        .expect_err("an order_by path with no matching join entry must error");
        assert!(err.contains("no"), "got {err}");
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
