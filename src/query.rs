use crate::state::{MODEL_REGISTRY, SqlDialect};
use ferro_schema_ir::{
    QueryIrPayload, QueryNode as QueryIrNode, QueryOrderBy as QueryIrOrderBy, QueryValue,
};
use sea_query::{Alias, Condition, Expr, SimpleExpr};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct QueryNode {
    pub is_compound: bool,
    pub operator: String,
    // Fields for simple node
    pub column: Option<String>,
    pub value: Option<Value>,
    // Fields for compound node
    pub left: Option<Box<QueryNode>>,
    pub right: Option<Box<QueryNode>>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct OrderBy {
    pub column: String,
    pub direction: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct M2mContext {
    pub join_table: String,
    pub source_col: String,
    pub target_col: String,
    pub source_id: Value,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct QueryDef {
    #[allow(dead_code)]
    pub model_name: String,
    pub where_clause: Vec<QueryNode>,
    pub order_by: Option<Vec<OrderBy>>,
    pub limit: Option<u64>,
    pub offset: Option<u64>,
    pub m2m: Option<M2mContext>,
    /// Populated from `pg_catalog` before building filter SQL. Not part of the
    /// Python query JSON payload.
    #[serde(skip)]
    pub postgres_enum_udt: HashMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuerySemanticSignature {
    pub model_name: String,
    pub where_semantics: Vec<String>,
    pub order_by: Vec<(String, String)>,
    pub limit: Option<u64>,
    pub offset: Option<u64>,
    pub m2m: Option<(String, String, String, String)>,
}

impl QueryDef {
    pub fn to_condition_for_backend(&self, backend: SqlDialect) -> Condition {
        let mut condition = Condition::all();
        for node in &self.where_clause {
            condition = condition.add(self.node_to_condition_for_backend(node, backend));
        }
        condition
    }

    fn node_to_condition_for_backend(&self, node: &QueryNode, backend: SqlDialect) -> Condition {
        if node.is_compound {
            let left_cond =
                self.node_to_condition_for_backend(node.left.as_ref().unwrap(), backend);
            let right_cond =
                self.node_to_condition_for_backend(node.right.as_ref().unwrap(), backend);

            match node.operator.as_str() {
                "OR" => Condition::any().add(left_cond).add(right_cond),
                "AND" => Condition::all().add(left_cond).add(right_cond),
                _ => Condition::all(), // Should not happen
            }
        } else {
            let col_name = node.column.as_ref().unwrap();
            let col = Expr::col(Alias::new(col_name));

            // Python `None` becomes JSON `null`, which serde deserializes as
            // `Option<serde_json::Value>::None` (not `Some(Null)`). SQL `col = NULL`
            // is never true — use `IS NULL` / `IS NOT NULL` for `== None` / `!= None`.
            let rhs_is_json_null = node.value.as_ref().is_none_or(serde_json::Value::is_null);

            let expr: SimpleExpr = if rhs_is_json_null {
                match node.operator.as_str() {
                    "==" => col.is_null(),
                    "!=" => col.is_not_null(),
                    "<" => col.lt(self.value_rhs_simple_expr_for_backend(
                        col_name,
                        &Value::Null,
                        false,
                        backend,
                    )),
                    "<=" => col.lte(self.value_rhs_simple_expr_for_backend(
                        col_name,
                        &Value::Null,
                        false,
                        backend,
                    )),
                    ">" => col.gt(self.value_rhs_simple_expr_for_backend(
                        col_name,
                        &Value::Null,
                        false,
                        backend,
                    )),
                    ">=" => col.gte(self.value_rhs_simple_expr_for_backend(
                        col_name,
                        &Value::Null,
                        false,
                        backend,
                    )),
                    "IN" => {
                        let val = node.value.as_ref().unwrap_or(&Value::Null);
                        if let Some(vals) = val.as_array() {
                            let rhs: Vec<SimpleExpr> = vals
                                .iter()
                                .map(|v| {
                                    self.value_rhs_simple_expr_for_backend(
                                        col_name, v, false, backend,
                                    )
                                })
                                .collect();
                            col.is_in(rhs)
                        } else {
                            col.eq(self
                                .value_rhs_simple_expr_for_backend(col_name, val, false, backend))
                        }
                    }
                    "LIKE" => {
                        let val = node.value.as_ref().unwrap_or(&Value::Null);
                        let pattern = match val {
                            Value::String(s) => s.clone(),
                            _ => val.to_string(),
                        };
                        col.like(pattern)
                    }
                    _ => col.eq(self.value_rhs_simple_expr_for_backend(
                        col_name,
                        &Value::Null,
                        false,
                        backend,
                    )),
                }
            } else {
                let val = node.value.as_ref().unwrap();
                match node.operator.as_str() {
                    "==" => col
                        .eq(self.value_rhs_simple_expr_for_backend(col_name, val, false, backend)),
                    "!=" => col
                        .ne(self.value_rhs_simple_expr_for_backend(col_name, val, false, backend)),
                    "<" => col
                        .lt(self.value_rhs_simple_expr_for_backend(col_name, val, false, backend)),
                    "<=" => col
                        .lte(self.value_rhs_simple_expr_for_backend(col_name, val, false, backend)),
                    ">" => col
                        .gt(self.value_rhs_simple_expr_for_backend(col_name, val, false, backend)),
                    ">=" => col
                        .gte(self.value_rhs_simple_expr_for_backend(col_name, val, false, backend)),
                    "IN" => {
                        if let Some(vals) = val.as_array() {
                            let rhs: Vec<SimpleExpr> = vals
                                .iter()
                                .map(|v| {
                                    self.value_rhs_simple_expr_for_backend(
                                        col_name, v, false, backend,
                                    )
                                })
                                .collect();
                            col.is_in(rhs)
                        } else {
                            col.eq(self
                                .value_rhs_simple_expr_for_backend(col_name, val, false, backend))
                        }
                    }
                    "LIKE" => {
                        let pattern = match val {
                            Value::String(s) => s.clone(),
                            _ => val.to_string(),
                        };
                        col.like(pattern)
                    }
                    _ => col
                        .eq(self.value_rhs_simple_expr_for_backend(col_name, val, false, backend)),
                }
            };
            Condition::all().add(expr)
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
        backend: SqlDialect,
    ) -> SimpleExpr {
        if let Value::String(s) = val {
            if backend == SqlDialect::Postgres {
                if let Some(tn) = crate::schema_bind::native_postgres_enum_udt_name(
                    col_name,
                    &self.postgres_enum_udt,
                ) {
                    return crate::schema_bind::postgres_enum_string_rhs_expr(s, tn);
                }

                if let Ok(parsed) = uuid::Uuid::parse_str(s) {
                    let schema_uuid = model_column_is_uuid(&self.model_name, col_name);
                    if schema_uuid || infer_uuid_without_schema {
                        return Expr::value(sea_query::Value::Uuid(Some(Box::new(parsed))));
                    }
                }

                if model_column_format(&self.model_name, col_name) == Some("date-time") {
                    return Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
                        .cast_as("timestamptz");
                }
                if model_column_format(&self.model_name, col_name) == Some("date") {
                    return Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
                        .cast_as("date");
                }
                if model_column_format(&self.model_name, col_name) == Some("binary") {
                    return Expr::value(sea_query::Value::Bytes(Some(Box::new(
                        s.as_bytes().to_vec(),
                    ))));
                }
                if model_column_is_decimal(&self.model_name, col_name) {
                    return Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
                        .cast_as("numeric");
                }
            }

            if model_column_is_decimal(&self.model_name, col_name)
                && let Ok(parsed) = s.parse::<f64>()
            {
                return Expr::value(sea_query::Value::Double(Some(parsed)));
            }
        }

        // Typed-null pick from column metadata. This fixes the schema-driven
        // half of #38 for UPDATE column values and query-filter predicates;
        // without it, every NULL bind goes out as text. Unknown columns fall
        // through to json_to_sea_value (which preserves legacy String(None))
        // -- the bind layer's NullKind::String is the documented fallback.
        if val.is_null()
            && let Some(typed_null) = typed_null_for_column(&self.model_name, col_name)
        {
            return Expr::value(typed_null);
        }
        Expr::value(json_to_sea_value(val))
    }

    pub fn to_ir_payload(&self) -> QueryIrPayload {
        QueryIrPayload {
            model_name: self.model_name.clone(),
            where_clause: self.where_clause.iter().map(query_node_to_ir).collect(),
            order_by: self
                .order_by
                .as_ref()
                .map(|items| {
                    items
                        .iter()
                        .map(|item| QueryIrOrderBy {
                            column: item.column.clone(),
                            direction: item.direction.clone(),
                        })
                        .collect()
                })
                .unwrap_or_default(),
            limit: self.limit,
            offset: self.offset,
            m2m: self
                .m2m
                .as_ref()
                .and_then(|m2m| serde_json::to_value(m2m).ok()),
        }
    }

    pub fn semantic_signature(&self) -> QuerySemanticSignature {
        QuerySemanticSignature {
            model_name: self.model_name.clone(),
            where_semantics: self
                .where_clause
                .iter()
                .map(query_node_semantic_string)
                .collect(),
            order_by: self
                .order_by
                .as_ref()
                .map(|items| {
                    items
                        .iter()
                        .map(|item| (item.column.clone(), item.direction.to_ascii_lowercase()))
                        .collect()
                })
                .unwrap_or_default(),
            limit: self.limit,
            offset: self.offset,
            m2m: self.m2m.as_ref().map(|m2m| {
                (
                    m2m.join_table.clone(),
                    m2m.source_col.clone(),
                    m2m.target_col.clone(),
                    query_value_semantic_string(&m2m.source_id),
                )
            }),
        }
    }
}

pub fn query_def_from_ir_payload(payload: QueryIrPayload) -> Result<QueryDef, String> {
    let m2m: Option<M2mContext> = match payload.m2m {
        Some(value) => serde_json::from_value(value)
            .map(Some)
            .map_err(|e| format!("invalid QueryIR m2m payload: {e}"))?,
        None => None,
    };
    Ok(QueryDef {
        model_name: payload.model_name,
        where_clause: payload
            .where_clause
            .iter()
            .map(query_node_from_ir)
            .collect(),
        order_by: if payload.order_by.is_empty() {
            None
        } else {
            Some(
                payload
                    .order_by
                    .iter()
                    .map(|item| OrderBy {
                        column: item.column.clone(),
                        direction: item.direction.clone(),
                    })
                    .collect(),
            )
        },
        limit: payload.limit,
        offset: payload.offset,
        m2m,
        postgres_enum_udt: HashMap::new(),
    })
}

fn query_node_to_ir(node: &QueryNode) -> QueryIrNode {
    if node.is_compound {
        let left = node
            .left
            .as_ref()
            .map(|inner| Box::new(query_node_to_ir(inner)))
            .unwrap_or_else(|| {
                Box::new(QueryIrNode::Leaf {
                    operator: "==".to_string(),
                    column: "__invalid__".to_string(),
                    value: QueryValue {
                        kind: "null".to_string(),
                        value: Value::Null,
                    },
                })
            });
        let right = node
            .right
            .as_ref()
            .map(|inner| Box::new(query_node_to_ir(inner)))
            .unwrap_or_else(|| {
                Box::new(QueryIrNode::Leaf {
                    operator: "==".to_string(),
                    column: "__invalid__".to_string(),
                    value: QueryValue {
                        kind: "null".to_string(),
                        value: Value::Null,
                    },
                })
            });
        return QueryIrNode::Compound {
            operator: node.operator.clone(),
            left,
            right,
        };
    }

    let value = node.value.clone().unwrap_or(Value::Null);
    QueryIrNode::Leaf {
        operator: node.operator.clone(),
        column: node.column.clone().unwrap_or_default(),
        value: QueryValue {
            kind: query_value_kind(&value).to_string(),
            value,
        },
    }
}

fn query_node_from_ir(node: &QueryIrNode) -> QueryNode {
    match node {
        QueryIrNode::Leaf {
            operator,
            column,
            value,
        } => QueryNode {
            is_compound: false,
            operator: operator.clone(),
            column: Some(column.clone()),
            value: if value.value.is_null() {
                None
            } else {
                Some(value.value.clone())
            },
            left: None,
            right: None,
        },
        QueryIrNode::Compound {
            operator,
            left,
            right,
        } => QueryNode {
            is_compound: true,
            operator: operator.clone(),
            column: None,
            value: None,
            left: Some(Box::new(query_node_from_ir(left))),
            right: Some(Box::new(query_node_from_ir(right))),
        },
    }
}

fn query_value_kind(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(n) => {
            if n.is_i64() || n.is_u64() {
                "int"
            } else {
                "float"
            }
        }
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

fn query_node_semantic_string(node: &QueryNode) -> String {
    if node.is_compound {
        let left = node
            .left
            .as_ref()
            .map(|inner| query_node_semantic_string(inner))
            .unwrap_or_else(|| "<missing-left>".to_string());
        let right = node
            .right
            .as_ref()
            .map(|inner| query_node_semantic_string(inner))
            .unwrap_or_else(|| "<missing-right>".to_string());
        return format!("({left} {} {right})", node.operator.to_ascii_uppercase());
    }

    let column = node
        .column
        .as_ref()
        .map_or_else(|| "<missing-column>".to_string(), Clone::clone);
    let value = node
        .value
        .as_ref()
        .map_or_else(|| "null".to_string(), query_value_semantic_string);
    format!("{} {} {}", column, node.operator, value)
}

fn query_value_semantic_string(value: &Value) -> String {
    match value {
        Value::String(s) => format!("\"{s}\""),
        Value::Null => "null".to_string(),
        _ => value.to_string(),
    }
}

/// Pick a typed SeaQuery `None` variant for a `NULL` value in
/// `value_rhs_simple_expr_for_backend`. Returns `None` if the model or column
/// isn't in the registry, or if the column type is one we still emit via
/// `CAST` (temporal). The caller falls through to `json_to_sea_value` in
/// either case, which preserves the legacy text-typed null.
fn typed_null_for_column(model_name: &str, col_name: &str) -> Option<sea_query::Value> {
    let registry = MODEL_REGISTRY.read().ok()?;
    let schema = registry.get(model_name)?;
    let props = schema.get("properties").and_then(|p| p.as_object())?;
    let col_info = props.get(col_name)?;

    if column_is_uuid_property(schema, col_name) {
        return Some(sea_query::Value::Uuid(None));
    }
    if property_schema_is_decimal(col_info) {
        // Decimal still uses cast_as("numeric") for non-null today; matching
        // null path keeps emitter behavior aligned. Native numeric typed bind
        // is deferred (plan §3 Scope Boundaries).
        return Some(sea_query::Value::Double(None));
    }
    let format = property_schema_format(col_info);
    if matches!(format, Some("date-time") | Some("date") | Some("time")) {
        // Temporal typed nulls are deferred to issue #40.
        return None;
    }
    if format == Some("binary") {
        return Some(sea_query::Value::Bytes(None));
    }

    // Walk anyOf to find the non-null type variant -- this is how Pydantic
    // shapes `T | None` schemas.
    let json_type = col_info.get("type").and_then(|t| t.as_str()).or_else(|| {
        col_info
            .get("anyOf")
            .and_then(|a| a.as_array())
            .and_then(|types| {
                types.iter().find_map(|t| {
                    let s = t.get("type")?.as_str()?;
                    if s != "null" { Some(s) } else { None }
                })
            })
    });

    match json_type {
        Some("integer") => Some(sea_query::Value::BigInt(None)),
        Some("number") => Some(sea_query::Value::Double(None)),
        Some("boolean") => Some(sea_query::Value::Bool(None)),
        Some("string") => Some(sea_query::Value::String(None)),
        _ => None,
    }
}

fn json_to_sea_value(value: &Value) -> sea_query::Value {
    match value {
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                sea_query::Value::BigInt(Some(i))
            } else if let Some(f) = n.as_f64() {
                sea_query::Value::Double(Some(f))
            } else {
                sea_query::Value::String(None)
            }
        }
        Value::String(s) => sea_query::Value::String(Some(Box::new(s.clone()))),
        Value::Bool(b) => sea_query::Value::Bool(Some(*b)),
        Value::Null => sea_query::Value::String(None),
        _ => sea_query::Value::String(Some(Box::new(value.to_string()))),
    }
}

fn model_column_is_uuid(model_name: &str, col: &str) -> bool {
    let Ok(registry) = MODEL_REGISTRY.read() else {
        return false;
    };
    let Some(schema) = registry.get(model_name) else {
        return false;
    };
    column_is_uuid_property(schema, col)
}

fn model_column_is_decimal(model_name: &str, col: &str) -> bool {
    let Ok(registry) = MODEL_REGISTRY.read() else {
        return false;
    };
    let Some(schema) = registry.get(model_name) else {
        return false;
    };
    let Some(props) = schema.get("properties").and_then(|p| p.as_object()) else {
        return false;
    };
    let Some(col_info) = props.get(col) else {
        return false;
    };
    property_schema_is_decimal(col_info)
}

fn model_column_format(model_name: &str, col: &str) -> Option<&'static str> {
    let Ok(registry) = MODEL_REGISTRY.read() else {
        return None;
    };
    let schema = registry.get(model_name)?;
    let props = schema.get("properties").and_then(|p| p.as_object())?;
    let col_info = props.get(col)?;
    match property_schema_format(col_info) {
        Some("uuid") => Some("uuid"),
        Some("date-time") => Some("date-time"),
        Some("date") => Some("date"),
        Some("binary") => Some("binary"),
        _ => None,
    }
}

fn column_is_uuid_property(schema: &Value, col: &str) -> bool {
    let Some(props) = schema.get("properties").and_then(|p| p.as_object()) else {
        return false;
    };
    let Some(col_info) = props.get(col) else {
        return false;
    };
    property_schema_is_uuid(col_info)
}

pub(crate) fn property_schema_format(col_info: &Value) -> Option<&str> {
    col_info.get("format").and_then(|f| f.as_str()).or_else(|| {
        col_info
            .get("anyOf")
            .and_then(|a| a.as_array())
            .and_then(|types| {
                types
                    .iter()
                    .find_map(|t| t.get("format").and_then(|f| f.as_str()))
            })
    })
}

pub(crate) fn property_schema_is_decimal(col_info: &Value) -> bool {
    col_info
        .get("anyOf")
        .and_then(|a| a.as_array())
        .map(|types| {
            let has_number = types
                .iter()
                .any(|t| t.get("type").and_then(|ty| ty.as_str()) == Some("number"));
            let has_patterned_string = types.iter().any(|t| {
                t.get("type").and_then(|ty| ty.as_str()) == Some("string")
                    && t.get("pattern").is_some()
            });
            has_number && has_patterned_string
        })
        .unwrap_or(false)
}

/// JSON Schema fragment for one model field: `string` + `format: uuid` (incl. optional `anyOf`).
pub(crate) fn property_schema_is_uuid(col_info: &Value) -> bool {
    let format = property_schema_format(col_info);
    let json_type = col_info.get("type").and_then(|t| t.as_str()).or_else(|| {
        col_info
            .get("anyOf")
            .and_then(|a| a.as_array())
            .and_then(|types| {
                types.iter().find_map(|t| {
                    let s = t.get("type")?.as_str()?;
                    if s == "null" { None } else { Some(s) }
                })
            })
    });
    json_type == Some("string") && format == Some("uuid")
}

#[cfg(test)]
mod tests {
    use super::{QueryDef, QueryNode};
    use crate::backend::BackendKind;
    use sea_query::{Alias, PostgresQueryBuilder, Query, SqliteQueryBuilder, Value as SeaValue};
    use serde_json::json;
    use std::collections::HashMap;

    fn empty_query_def(model_name: &str) -> QueryDef {
        QueryDef {
            model_name: model_name.to_string(),
            where_clause: Vec::new(),
            order_by: None,
            limit: None,
            offset: None,
            m2m: None,
            postgres_enum_udt: HashMap::new(),
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
    fn json_null_deserializes_to_option_none_for_query_node_value() {
        let node: QueryNode = serde_json::from_value(json!({
            "is_compound": false,
            "column": "count",
            "operator": "==",
            "value": null
        }))
        .unwrap();
        assert!(node.value.is_none());
    }

    #[test]
    fn where_rhs_none_emits_is_null_for_eq_sqlite() {
        let node: QueryNode = serde_json::from_value(json!({
            "is_compound": false,
            "column": "attached_at",
            "operator": "==",
            "value": null
        }))
        .unwrap();
        let q = QueryDef {
            model_name: "Pending".to_string(),
            where_clause: vec![node],
            order_by: None,
            limit: None,
            offset: None,
            m2m: None,
            postgres_enum_udt: HashMap::new(),
        };
        let mut select = Query::select();
        select
            .from(Alias::new("pending"))
            .cond_where(q.to_condition_for_backend(BackendKind::Sqlite));
        let sql = select.to_string(SqliteQueryBuilder).to_lowercase();
        assert!(sql.contains("is null"), "expected IS NULL, got {sql}");
        assert!(
            !sql.contains("= null"),
            "must not emit `= NULL` (always unknown in SQL): {sql}"
        );
    }

    #[test]
    fn where_rhs_none_emits_is_not_null_for_ne_sqlite() {
        let node: QueryNode = serde_json::from_value(json!({
            "is_compound": false,
            "column": "payload",
            "operator": "!=",
            "value": null
        }))
        .unwrap();
        let q = QueryDef {
            model_name: "Pending".to_string(),
            where_clause: vec![node],
            order_by: None,
            limit: None,
            offset: None,
            m2m: None,
            postgres_enum_udt: HashMap::new(),
        };
        let mut select = Query::select();
        select
            .from(Alias::new("pending"))
            .cond_where(q.to_condition_for_backend(BackendKind::Sqlite));
        let sql = select.to_string(SqliteQueryBuilder).to_lowercase();
        assert!(
            sql.contains("is not null"),
            "expected IS NOT NULL, got {sql}"
        );
    }

    #[test]
    fn uuid_rhs_emits_typed_uuid_bind_on_postgres_no_cast() {
        let query_def = empty_query_def("Widget");
        let uuid_str = "3f4c4ca7-a7e7-40d6-8d83-8f4ddf3285e6";

        let postgres_rhs = query_def.value_rhs_simple_expr_for_backend(
            "widget_id",
            &json!(uuid_str),
            true,
            BackendKind::Postgres,
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
        let query_def = empty_query_def("Widget");
        let uuid_str = "3f4c4ca7-a7e7-40d6-8d83-8f4ddf3285e6";

        let sqlite_rhs = query_def.value_rhs_simple_expr_for_backend(
            "widget_id",
            &json!(uuid_str),
            true,
            BackendKind::Sqlite,
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
            json!({
                "properties": {
                    "count": {"anyOf": [{"type": "integer"}, {"type": "null"}]}
                }
            }),
        );
        let query_def = empty_query_def("WidgetIntNull");

        let rhs = query_def.value_rhs_simple_expr_for_backend(
            "count",
            &serde_json::Value::Null,
            false,
            BackendKind::Postgres,
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
            json!({
                "properties": {
                    "active": {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
                }
            }),
        );
        let query_def = empty_query_def("WidgetBoolNull");

        let rhs = query_def.value_rhs_simple_expr_for_backend(
            "active",
            &serde_json::Value::Null,
            false,
            BackendKind::Postgres,
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
            json!({
                "properties": {
                    "id": {"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]}
                }
            }),
        );
        let query_def = empty_query_def("WidgetUuidNull");

        let rhs = query_def.value_rhs_simple_expr_for_backend(
            "id",
            &serde_json::Value::Null,
            false,
            BackendKind::Postgres,
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
            json!({
                "properties": {
                    "blob": {"type": "string", "format": "binary"}
                }
            }),
        );
        let query_def = empty_query_def("WidgetBinary");

        let rhs = query_def.value_rhs_simple_expr_for_backend(
            "blob",
            &json!("some-bytes"),
            false,
            BackendKind::Postgres,
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
        let mut query_def = empty_query_def("WidgetColor");
        query_def
            .postgres_enum_udt
            .insert("color".to_string(), "color".to_string());

        let rhs = query_def.value_rhs_simple_expr_for_backend(
            "color",
            &json!("red"),
            false,
            BackendKind::Postgres,
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
            json!({
                "properties": {
                    "color": {"enum_type_name": "color", "db_type": "text"}
                }
            }),
        );
        let query_def = empty_query_def("WidgetTextColor");

        let rhs = query_def.value_rhs_simple_expr_for_backend(
            "color",
            &json!("red"),
            false,
            BackendKind::Postgres,
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
            json!({
                "properties": {
                    "amount": {
                        "anyOf": [
                            {"type": "number"},
                            {"type": "string", "pattern": "^-?\\d+(\\.\\d+)?$"}
                        ]
                    }
                }
            }),
        );
        let query_def = empty_query_def("WidgetDecimal");

        let rhs = query_def.value_rhs_simple_expr_for_backend(
            "amount",
            &json!("12.34"),
            false,
            BackendKind::Postgres,
        );
        let sql = Query::select().expr(rhs).to_string(PostgresQueryBuilder);

        assert!(
            sql.contains("AS numeric"),
            "decimal cast preserved until follow-up: {sql}"
        );
    }

    #[test]
    fn query_ir_roundtrip_preserves_semantics_signature() {
        let query_def: QueryDef = serde_json::from_value(json!({
            "model_name": "Widget",
            "where_clause": [
                {
                    "is_compound": true,
                    "operator": "OR",
                    "left": {
                        "is_compound": false,
                        "column": "age",
                        "operator": ">=",
                        "value": 18
                    },
                    "right": {
                        "is_compound": false,
                        "column": "name",
                        "operator": "LIKE",
                        "value": "a%"
                    }
                }
            ],
            "order_by": [{"column": "age", "direction": "DESC"}],
            "limit": 10,
            "offset": 5,
            "m2m": null
        }))
        .expect("query json must deserialize");
        let before = query_def.semantic_signature();
        let ir = query_def.to_ir_payload();
        let roundtrip = super::query_def_from_ir_payload(ir).expect("QueryIR roundtrip");
        let after = roundtrip.semantic_signature();

        assert_eq!(before, after);
    }
}
