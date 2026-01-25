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
pub struct QueryDef {
    pub model_name: String,
    pub where_clause: Vec<QueryNode>,
    pub order_by: Option<Vec<OrderBy>>,
    pub limit: Option<u64>,
    pub offset: Option<u64>,
}

impl QueryDef {
    pub fn to_condition(&self) -> Condition {
        let mut condition = Condition::all();
        for node in &self.where_clause {
            condition = condition.add(self.node_to_condition(node));
        }
        condition
    }

    fn node_to_condition(&self, node: &QueryNode) -> Condition {
        if node.is_compound {
            let left_cond = self.node_to_condition(node.left.as_ref().unwrap());
            let right_cond = self.node_to_condition(node.right.as_ref().unwrap());

            match node.operator.as_str() {
                "OR" => Condition::any().add(left_cond).add(right_cond),
                "AND" => Condition::all().add(left_cond).add(right_cond),
                _ => Condition::all(), // Should not happen
            }
        } else {
            let col_name = node.column.as_ref().unwrap();
            let val = node.value.as_ref().unwrap();
            let col = Expr::col(Alias::new(col_name));

            let expr: SimpleExpr = match node.operator.as_str() {
                "==" => col.eq(self.value_to_sea_value(val)),
                "!=" => col.ne(self.value_to_sea_value(val)),
                "<" => col.lt(self.value_to_sea_value(val)),
                "<=" => col.lte(self.value_to_sea_value(val)),
                ">" => col.gt(self.value_to_sea_value(val)),
                ">=" => col.gte(self.value_to_sea_value(val)),
                "IN" => {
                    if let Some(vals) = val.as_array() {
                        let sea_vals: Vec<sea_query::Value> =
                            vals.iter().map(|v| self.value_to_sea_value(v)).collect();
                        col.is_in(sea_vals)
                    } else {
                        col.eq(self.value_to_sea_value(val))
                    }
                }
                "LIKE" => {
                    let pattern = match val {
                        Value::String(s) => s.clone(),
                        _ => val.to_string(),
                    };
                    col.like(pattern)
                }
                _ => col.eq(self.value_to_sea_value(val)),
            };
            Condition::all().add(expr)
        }
    }

    fn value_to_sea_value(&self, value: &Value) -> sea_query::Value {
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
}
