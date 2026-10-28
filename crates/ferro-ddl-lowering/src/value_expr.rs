//! Value expressions: the function table and the one renderer (ADR-0059).
//!
//! A generated column's body is a [`ValueExpr`] over the row's own columns,
//! literals, `+ - * /`, unary `-` and the functions in [`VALUE_FUNCTIONS`].
//! The table is the single decision of which functions exist (I-1): Python
//! reads it over FFI to build `ferro.fn`, and [`render_value_expr`] renders
//! every call per dialect.

use std::collections::BTreeSet;

use ferro_schema_ir::{ValueBinaryOp, ValueExpr};
use serde::Serialize;

use crate::{Dialect, quote_ident};

/// The value families a function admits and returns.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ValueType {
    Integer,
    Number,
    Decimal,
    String,
    Date,
    Datetime,
    Boolean,
}

/// One accepted call shape of a function.
#[derive(Clone, Copy, Debug, Serialize)]
pub struct ValueSignature {
    /// The argument types, in order.
    pub args: &'static [ValueType],
    /// `true` when the last argument type repeats.
    pub variadic: bool,
    /// The type the call returns.
    pub result: ValueType,
}

/// One function of the value function table.
#[derive(Clone, Copy, Debug, Serialize)]
pub struct ValueFunction {
    /// The name `ferro.fn` exposes and the IR carries.
    pub name: &'static str,
    /// Every call shape the function accepts.
    pub signatures: &'static [ValueSignature],
}

/// The closed value function table (ADR-0059).
pub const VALUE_FUNCTIONS: &[ValueFunction] = &[ValueFunction {
    name: "sign",
    signatures: &[
        ValueSignature {
            args: &[ValueType::Integer],
            variadic: false,
            result: ValueType::Integer,
        },
        ValueSignature {
            args: &[ValueType::Number],
            variadic: false,
            result: ValueType::Number,
        },
        ValueSignature {
            args: &[ValueType::Decimal],
            variadic: false,
            result: ValueType::Decimal,
        },
    ],
}];

/// The function table entry named `name`.
pub fn value_function(name: &str) -> Option<&'static ValueFunction> {
    VALUE_FUNCTIONS.iter().find(|function| function.name == name)
}

/// Render a value expression for `dialect`.
///
/// Every binary node is parenthesised and unary minus renders as `(- x)`, so
/// the rendering never depends on operator precedence and never forms `--`.
///
/// # Errors
/// A message naming the function when a call names one the table lacks.
pub fn render_value_expr(expr: &ValueExpr, dialect: Dialect) -> Result<String, String> {
    Ok(match expr {
        ValueExpr::Column { name } => quote_ident(name),
        ValueExpr::Literal { token } => token.clone(),
        ValueExpr::Binary { op, left, right } => format!(
            "({} {} {})",
            render_value_expr(left, dialect)?,
            binary_op_sql(*op),
            render_value_expr(right, dialect)?
        ),
        ValueExpr::Negate { operand } => format!("(- {})", render_value_expr(operand, dialect)?),
        ValueExpr::Func { name, args } => {
            let function = value_function(name).ok_or_else(|| {
                let known: Vec<&str> = VALUE_FUNCTIONS.iter().map(|f| f.name).collect();
                format!(
                    "unknown value function '{name}'; the functions are: {}",
                    known.join(", ")
                )
            })?;
            let rendered = args
                .iter()
                .map(|arg| render_value_expr(arg, dialect))
                .collect::<Result<Vec<_>, _>>()?;
            render_call(function, &rendered, dialect)
        }
    })
}

/// One function call's SQL per dialect. Every function in the table so far
/// spells the same on both databases.
fn render_call(function: &ValueFunction, args: &[String], _dialect: Dialect) -> String {
    format!("{}({})", function.name, args.join(", "))
}

fn binary_op_sql(op: ValueBinaryOp) -> &'static str {
    match op {
        ValueBinaryOp::Add => "+",
        ValueBinaryOp::Sub => "-",
        ValueBinaryOp::Mul => "*",
        ValueBinaryOp::Div => "/",
    }
}

/// The column clause of a generated column: `GENERATED ALWAYS AS (<expr>)
/// STORED`. `STORED` is always written: Postgres 18 defaults to `VIRTUAL`.
///
/// # Errors
/// As [`render_value_expr`].
pub fn render_generated_clause(expr: &ValueExpr, dialect: Dialect) -> Result<String, String> {
    Ok(format!(
        "GENERATED ALWAYS AS ({}) STORED",
        render_value_expr(expr, dialect)?
    ))
}

/// Every column the expression reads.
pub fn value_expr_columns(expr: &ValueExpr) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    collect_columns(expr, &mut out);
    out
}

fn collect_columns(expr: &ValueExpr, out: &mut BTreeSet<String>) {
    match expr {
        ValueExpr::Column { name } => {
            out.insert(name.clone());
        }
        ValueExpr::Literal { .. } => {}
        ValueExpr::Binary { left, right, .. } => {
            collect_columns(left, out);
            collect_columns(right, out);
        }
        ValueExpr::Negate { operand } => collect_columns(operand, out),
        ValueExpr::Func { args, .. } => args.iter().for_each(|arg| collect_columns(arg, out)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn column(name: &str) -> Box<ValueExpr> {
        Box::new(ValueExpr::Column {
            name: name.to_string(),
        })
    }

    fn literal(token: &str) -> Box<ValueExpr> {
        Box::new(ValueExpr::Literal {
            token: token.to_string(),
        })
    }

    const DIALECTS: [Dialect; 2] = [Dialect::Sqlite, Dialect::Postgres];

    #[test]
    fn each_operator_renders_parenthesised_on_both_dialects() {
        for (op, symbol) in [
            (ValueBinaryOp::Add, "+"),
            (ValueBinaryOp::Sub, "-"),
            (ValueBinaryOp::Mul, "*"),
            (ValueBinaryOp::Div, "/"),
        ] {
            let expr = ValueExpr::Binary {
                op,
                left: column("a"),
                right: literal("2"),
            };
            for dialect in DIALECTS {
                assert_eq!(
                    render_value_expr(&expr, dialect).unwrap(),
                    format!("(\"a\" {symbol} 2)")
                );
            }
        }
    }

    #[test]
    fn unary_minus_never_forms_a_comment() {
        let expr = ValueExpr::Negate {
            operand: literal("-5"),
        };
        for dialect in DIALECTS {
            assert_eq!(render_value_expr(&expr, dialect).unwrap(), "(- -5)");
        }
    }

    #[test]
    fn sign_renders_the_same_on_both_dialects_with_its_clause() {
        let expr = ValueExpr::Func {
            name: "sign".to_string(),
            args: vec![*column("amount_minor")],
        };
        for dialect in DIALECTS {
            assert_eq!(
                render_generated_clause(&expr, dialect).unwrap(),
                "GENERATED ALWAYS AS (sign(\"amount_minor\")) STORED"
            );
        }
    }

    #[test]
    fn an_unknown_function_is_an_error_naming_it() {
        let expr = ValueExpr::Func {
            name: "nope".to_string(),
            args: vec![],
        };
        let err = render_value_expr(&expr, Dialect::Postgres).unwrap_err();
        assert!(err.contains("'nope'") && err.contains("sign"), "{err}");
    }

    #[test]
    fn columns_are_collected_from_every_branch() {
        let expr = ValueExpr::Binary {
            op: ValueBinaryOp::Mul,
            left: Box::new(ValueExpr::Negate {
                operand: column("b"),
            }),
            right: Box::new(ValueExpr::Func {
                name: "sign".to_string(),
                args: vec![*column("a")],
            }),
        };
        assert_eq!(
            value_expr_columns(&expr).into_iter().collect::<Vec<_>>(),
            ["a", "b"]
        );
    }

    #[test]
    fn the_table_serializes_its_type_tokens() {
        let json = serde_json::to_value(VALUE_FUNCTIONS).unwrap();
        assert_eq!(json[0]["name"], "sign");
        assert_eq!(json[0]["signatures"][0]["args"][0], "integer");
        assert_eq!(json[0]["signatures"][0]["result"], "integer");
        assert_eq!(json[0]["signatures"][0]["variadic"], false);
    }
}
