use crate::state::{MODEL_REGISTRY, SqlDialect};
use sea_query::{Alias, Condition, Expr, SimpleExpr};
use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Deserialize)]
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

#[derive(Debug, Deserialize)]
pub struct OrderBy {
    pub column: String,
    pub direction: String,
}

#[derive(Debug, Deserialize)]
pub struct M2mContext {
    pub join_table: String,
    pub source_col: String,
    pub target_col: String,
    pub source_id: Value,
}

#[derive(Debug, Deserialize)]
pub struct QueryDef {
    #[allow(dead_code)]
    pub model_name: String,
    pub where_clause: Vec<QueryNode>,
    pub order_by: Option<Vec<OrderBy>>,
    pub limit: Option<u64>,
    pub offset: Option<u64>,
    pub m2m: Option<M2mContext>,
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
            let val = node.value.as_ref().unwrap();
            let col = Expr::col(Alias::new(col_name));

            let expr: SimpleExpr =
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
                };
            Condition::all().add(expr)
        }
    }

    /// Right-hand side for a filter comparison.
    ///
    /// On Postgres, UUID columns compared to JSON string parameters need an explicit
    /// `CAST(... AS uuid)` so the comparison is `uuid = uuid`.
    ///
    /// `infer_uuid_without_schema` is used for M2M join filters where the RHS is a UUID
    /// string but the join column is not described on the queried model's schema.
    pub fn value_rhs_simple_expr_for_backend(
        &self,
        col_name: &str,
        val: &Value,
        infer_uuid_without_schema: bool,
        backend: SqlDialect,
    ) -> SimpleExpr {
        if let Value::String(s) = val {
            if backend == SqlDialect::Postgres {
                if uuid::Uuid::parse_str(s).is_ok() {
                    let schema_uuid = model_column_is_uuid(&self.model_name, col_name);
                    if schema_uuid || infer_uuid_without_schema {
                        return Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
                            .cast_as("uuid");
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
                if model_column_format(&self.model_name, col_name).as_deref() == Some("date") {
                    return Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
                        .cast_as("date");
                }
                if model_column_format(&self.model_name, col_name).as_deref() == Some("date-time") {
                    return Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
                        .cast_as("timestamptz");
                }
                if model_column_format(&self.model_name, col_name).as_deref() == Some("binary") {
                    return Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
                        .cast_as("bytea");
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
        Expr::value(json_to_sea_value(val))
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
    let Some(schema) = registry.get(model_name) else {
        return None;
    };
    let Some(props) = schema.get("properties").and_then(|p| p.as_object()) else {
        return None;
    };
    let Some(col_info) = props.get(col) else {
        return None;
    };
    match property_schema_format(col_info) {
        Some("uuid") => Some("uuid"),
        Some("date-time") => Some("date-time"),
        Some("date") => Some("date"),
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
    use super::QueryDef;
    use crate::backend::BackendKind;
    use sea_query::{PostgresQueryBuilder, Query, SqliteQueryBuilder};
    use serde_json::json;

    #[test]
    fn uuid_rhs_cast_uses_explicit_backend_not_global_default() {
        let query_def = QueryDef {
            model_name: "Widget".to_string(),
            where_clause: Vec::new(),
            order_by: None,
            limit: None,
            offset: None,
            m2m: None,
        };

        let postgres_rhs = query_def.value_rhs_simple_expr_for_backend(
            "widget_id",
            &json!("3f4c4ca7-a7e7-40d6-8d83-8f4ddf3285e6"),
            true,
            BackendKind::Postgres,
        );
        let postgres_sql = Query::select()
            .expr(postgres_rhs)
            .to_string(PostgresQueryBuilder);

        assert!(
            postgres_sql.contains("AS uuid"),
            "unexpected SQL: {postgres_sql}"
        );

        let sqlite_rhs = query_def.value_rhs_simple_expr_for_backend(
            "widget_id",
            &json!("3f4c4ca7-a7e7-40d6-8d83-8f4ddf3285e6"),
            true,
            BackendKind::Sqlite,
        );
        let sqlite_sql = Query::select()
            .expr(sqlite_rhs)
            .to_string(SqliteQueryBuilder);

        assert!(
            !sqlite_sql.contains("AS uuid"),
            "unexpected SQL: {sqlite_sql}"
        );
    }
}
