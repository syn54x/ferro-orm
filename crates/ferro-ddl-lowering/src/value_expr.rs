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

/// How a function treats a `NULL` argument.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum NullRule {
    /// The result is `NULL` when any argument is.
    Propagate,
    /// The result is `NULL` only when every argument is.
    FirstNonNull,
    /// Every argument must be non-nullable: the databases disagree on a
    /// `NULL` (`least(NULL, 28)` is `28` on Postgres and `NULL` on SQLite).
    Refused,
}

/// One function of the value function table.
#[derive(Clone, Copy, Debug, Serialize)]
pub struct ValueFunction {
    /// The name `ferro.fn` exposes and the IR carries.
    pub name: &'static str,
    /// Every call shape the function accepts.
    pub signatures: &'static [ValueSignature],
    /// How a `NULL` argument reaches the result.
    pub nulls: NullRule,
}

const fn one(arg: &'static [ValueType], result: ValueType) -> ValueSignature {
    ValueSignature {
        args: arg,
        variadic: false,
        result,
    }
}

const fn repeated(args: &'static [ValueType], result: ValueType) -> ValueSignature {
    ValueSignature {
        args,
        variadic: true,
        result,
    }
}

const NUMERIC_UNARY: &[ValueSignature] = &[
    one(&[ValueType::Integer], ValueType::Integer),
    one(&[ValueType::Number], ValueType::Number),
    one(&[ValueType::Decimal], ValueType::Decimal),
];

/// `least` and `greatest` take numbers and dates only: text compares by
/// collation on Postgres and byte by byte on SQLite.
const ORDERED_REPEATED: &[ValueSignature] = &[
    repeated(
        &[ValueType::Integer, ValueType::Integer],
        ValueType::Integer,
    ),
    repeated(&[ValueType::Number, ValueType::Number], ValueType::Number),
    repeated(
        &[ValueType::Decimal, ValueType::Decimal],
        ValueType::Decimal,
    ),
    repeated(&[ValueType::Date, ValueType::Date], ValueType::Date),
];

/// A part of a `date`. A `datetime` has no signature: its day depends on the
/// session's time zone, which Postgres refuses in a generated column.
const DATE_PART: &[ValueSignature] = &[one(&[ValueType::Date], ValueType::Integer)];

/// The closed value function table (ADR-0059). A function joins only when
/// SQLite and Postgres compute the same value for every signature, `NULL`
/// included; `tests/test_value_parity.py` runs each one on both.
pub const VALUE_FUNCTIONS: &[ValueFunction] = &[
    ValueFunction {
        name: "sign",
        signatures: NUMERIC_UNARY,
        nulls: NullRule::Propagate,
    },
    ValueFunction {
        name: "abs",
        signatures: NUMERIC_UNARY,
        nulls: NullRule::Propagate,
    },
    ValueFunction {
        name: "least",
        signatures: ORDERED_REPEATED,
        nulls: NullRule::Refused,
    },
    ValueFunction {
        name: "greatest",
        signatures: ORDERED_REPEATED,
        nulls: NullRule::Refused,
    },
    ValueFunction {
        name: "coalesce",
        signatures: &[
            repeated(
                &[ValueType::Integer, ValueType::Integer],
                ValueType::Integer,
            ),
            repeated(&[ValueType::Number, ValueType::Number], ValueType::Number),
            repeated(
                &[ValueType::Decimal, ValueType::Decimal],
                ValueType::Decimal,
            ),
            repeated(&[ValueType::String, ValueType::String], ValueType::String),
            repeated(&[ValueType::Date, ValueType::Date], ValueType::Date),
            repeated(
                &[ValueType::Datetime, ValueType::Datetime],
                ValueType::Datetime,
            ),
        ],
        nulls: NullRule::FirstNonNull,
    },
    ValueFunction {
        name: "length",
        signatures: &[one(&[ValueType::String], ValueType::Integer)],
        nulls: NullRule::Propagate,
    },
    ValueFunction {
        name: "year",
        signatures: DATE_PART,
        nulls: NullRule::Propagate,
    },
    ValueFunction {
        name: "month",
        signatures: DATE_PART,
        nulls: NullRule::Propagate,
    },
    ValueFunction {
        name: "day",
        signatures: DATE_PART,
        nulls: NullRule::Propagate,
    },
];

/// The function table entry named `name`.
pub fn value_function(name: &str) -> Option<&'static ValueFunction> {
    VALUE_FUNCTIONS
        .iter()
        .find(|function| function.name == name)
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

/// One function call's SQL per dialect.
fn render_call(function: &ValueFunction, args: &[String], dialect: Dialect) -> String {
    let joined = args.join(", ");
    // Postgres's `extract` returns `numeric`; the cast keeps `fn.day(d) / 2`
    // an integer division, as it is on SQLite.
    let date_part = |pg_field: &str, sqlite_format: &str| match dialect {
        Dialect::Postgres => format!("CAST(extract({pg_field} from {joined}) AS integer)"),
        Dialect::Sqlite => format!("CAST(strftime('{sqlite_format}', {joined}) AS integer)"),
    };
    match (function.name, dialect) {
        ("least", Dialect::Sqlite) => format!("min({joined})"),
        ("greatest", Dialect::Sqlite) => format!("max({joined})"),
        ("year", _) => date_part("year", "%Y"),
        ("month", _) => date_part("month", "%m"),
        ("day", _) => date_part("day", "%d"),
        (name, _) => format!("{name}({joined})"),
    }
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

/// The SQLite column check an integer generated column carries.
///
/// SQLite turns an overflowing integer result into a `real`, where Postgres
/// raises `bigint out of range`. The check makes SQLite raise too, so the
/// failure is the same on both (value parity covers failures). `NULL` passes,
/// as it does for every check.
pub fn render_sqlite_integer_guard(column: &str) -> String {
    format!(
        "CHECK (typeof({}) IN ('integer', 'null'))",
        quote_ident(column)
    )
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

    fn call(name: &str, args: &[&str]) -> ValueExpr {
        ValueExpr::Func {
            name: name.to_string(),
            args: args.iter().map(|arg| *column(arg)).collect(),
        }
    }

    fn renders(name: &str, args: &[&str], dialect: Dialect) -> String {
        render_value_expr(&call(name, args), dialect).unwrap()
    }

    #[test]
    fn sign_renders_sign_on_sqlite_and_postgres() {
        assert_eq!(renders("sign", &["a"], Dialect::Sqlite), "sign(\"a\")");
        assert_eq!(renders("sign", &["a"], Dialect::Postgres), "sign(\"a\")");
    }

    #[test]
    fn abs_renders_abs_on_sqlite_and_postgres() {
        assert_eq!(renders("abs", &["a"], Dialect::Sqlite), "abs(\"a\")");
        assert_eq!(renders("abs", &["a"], Dialect::Postgres), "abs(\"a\")");
    }

    #[test]
    fn least_renders_min_on_sqlite_and_least_on_postgres() {
        assert_eq!(
            renders("least", &["a", "b", "c"], Dialect::Sqlite),
            "min(\"a\", \"b\", \"c\")"
        );
        assert_eq!(
            renders("least", &["a", "b"], Dialect::Postgres),
            "least(\"a\", \"b\")"
        );
    }

    #[test]
    fn greatest_renders_max_on_sqlite_and_greatest_on_postgres() {
        assert_eq!(
            renders("greatest", &["a", "b"], Dialect::Sqlite),
            "max(\"a\", \"b\")"
        );
        assert_eq!(
            renders("greatest", &["a", "b"], Dialect::Postgres),
            "greatest(\"a\", \"b\")"
        );
    }

    #[test]
    fn coalesce_renders_coalesce_on_sqlite_and_postgres() {
        for dialect in DIALECTS {
            assert_eq!(
                renders("coalesce", &["a", "b"], dialect),
                "coalesce(\"a\", \"b\")"
            );
        }
    }

    #[test]
    fn length_renders_length_on_sqlite_and_postgres() {
        for dialect in DIALECTS {
            assert_eq!(renders("length", &["name"], dialect), "length(\"name\")");
        }
    }

    #[test]
    fn year_renders_strftime_on_sqlite_and_an_integer_extract_on_postgres() {
        assert_eq!(
            renders("year", &["d"], Dialect::Sqlite),
            "CAST(strftime('%Y', \"d\") AS integer)"
        );
        assert_eq!(
            renders("year", &["d"], Dialect::Postgres),
            "CAST(extract(year from \"d\") AS integer)"
        );
    }

    #[test]
    fn month_renders_strftime_on_sqlite_and_an_integer_extract_on_postgres() {
        assert_eq!(
            renders("month", &["d"], Dialect::Sqlite),
            "CAST(strftime('%m', \"d\") AS integer)"
        );
        assert_eq!(
            renders("month", &["d"], Dialect::Postgres),
            "CAST(extract(month from \"d\") AS integer)"
        );
    }

    #[test]
    fn day_renders_strftime_on_sqlite_and_an_integer_extract_on_postgres() {
        assert_eq!(
            renders("day", &["d"], Dialect::Sqlite),
            "CAST(strftime('%d', \"d\") AS integer)"
        );
        assert_eq!(
            renders("day", &["d"], Dialect::Postgres),
            "CAST(extract(day from \"d\") AS integer)"
        );
    }

    #[test]
    fn the_epic_day_class_nests_on_both_dialects() {
        let expr = ValueExpr::Func {
            name: "least".to_string(),
            args: vec![call("day", &["date"]), *literal("28")],
        };
        assert_eq!(
            render_value_expr(&expr, Dialect::Sqlite).unwrap(),
            "min(CAST(strftime('%d', \"date\") AS integer), 28)"
        );
        assert_eq!(
            render_value_expr(&expr, Dialect::Postgres).unwrap(),
            "least(CAST(extract(day from \"date\") AS integer), 28)"
        );
    }

    #[test]
    fn the_table_holds_the_v1_functions_with_their_null_rules() {
        let names: Vec<&str> = VALUE_FUNCTIONS.iter().map(|f| f.name).collect();
        assert_eq!(
            names,
            [
                "sign", "abs", "least", "greatest", "coalesce", "length", "year", "month", "day"
            ]
        );
        let json = serde_json::to_value(VALUE_FUNCTIONS).unwrap();
        assert_eq!(json[2]["nulls"], "refused");
        assert_eq!(json[4]["nulls"], "first_non_null");
        assert_eq!(json[0]["nulls"], "propagate");
    }

    #[test]
    fn least_and_greatest_admit_numbers_and_dates_only() {
        for name in ["least", "greatest"] {
            let function = value_function(name).unwrap();
            assert!(function.signatures.iter().all(|sig| sig.variadic
                && matches!(
                    sig.result,
                    ValueType::Integer | ValueType::Number | ValueType::Decimal | ValueType::Date
                )));
        }
    }

    #[test]
    fn the_sqlite_integer_guard_admits_integers_and_null() {
        assert_eq!(
            render_sqlite_integer_guard("total"),
            "CHECK (typeof(\"total\") IN ('integer', 'null'))"
        );
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
