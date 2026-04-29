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
                if model_column_format(&self.model_name, col_name).as_deref() == Some("binary") {
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
    let json_type = col_info
        .get("type")
        .and_then(|t| t.as_str())
        .or_else(|| {
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
    use super::QueryDef;
    use crate::backend::BackendKind;
    use sea_query::{
        Alias, PostgresQueryBuilder, Query, SqliteQueryBuilder, Value as SeaValue,
    };
    use serde_json::json;

    fn empty_query_def(model_name: &str) -> QueryDef {
        QueryDef {
            model_name: model_name.to_string(),
            where_clause: Vec::new(),
            order_by: None,
            limit: None,
            offset: None,
            m2m: None,
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
}
