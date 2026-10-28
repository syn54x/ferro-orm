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

use ferro_schema_ir::SchemaColumn;

use crate::{CanonicalType, Dialect, ResolvedStorage, quote_ident, resolve_column_storage};

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

/// Render a value expression for `dialect` over the table's `columns`.
///
/// Every binary node is parenthesised and unary minus renders as `(- x)`, so
/// the rendering never depends on operator precedence and never forms `--`.
///
/// On Postgres every integer value narrower than `bigint` (a `smallint` or
/// `integer` column, `length`, a date part) is cast to `bigint`. Postgres
/// computes integer arithmetic at its operands' width and SQLite at 64 bits,
/// so without the cast `a + b - c` over `integer` columns fails on Postgres
/// at a middle step that SQLite computes fine. With it, both compute at 64
/// bits and only the column's own range decides, as Postgres's assignment
/// to the column and the SQLite storage guard enforce.
///
/// # Errors
/// A message naming the function when a call names one the table lacks.
pub fn render_value_expr(
    expr: &ValueExpr,
    dialect: Dialect,
    columns: &[SchemaColumn],
) -> Result<String, String> {
    let render = |expr: &ValueExpr| render_value_expr(expr, dialect, columns);
    Ok(match expr {
        ValueExpr::Column { name } => {
            let quoted = quote_ident(name);
            let narrow = dialect == Dialect::Postgres
                && columns.iter().any(|col| {
                    col.name == *name
                        && matches!(
                            resolve_column_storage(col, Dialect::Postgres),
                            Ok(ResolvedStorage::Scalar(
                                CanonicalType::SmallInt | CanonicalType::Integer
                            ))
                        )
                });
            if narrow { widen(&quoted) } else { quoted }
        }
        ValueExpr::Literal { token } => token.clone(),
        ValueExpr::Binary { op, left, right } => format!(
            "({} {} {})",
            render(left)?,
            binary_op_sql(*op),
            render(right)?
        ),
        ValueExpr::Negate { operand } => format!("(- {})", render(operand)?),
        ValueExpr::Func { name, args } => {
            let function = value_function(name).ok_or_else(|| {
                let known: Vec<&str> = VALUE_FUNCTIONS.iter().map(|f| f.name).collect();
                format!(
                    "unknown value function '{name}'; the functions are: {}",
                    known.join(", ")
                )
            })?;
            let rendered = args.iter().map(render).collect::<Result<Vec<_>, _>>()?;
            render_call(function, &rendered, dialect)
        }
    })
}

/// A Postgres integer value at 64 bits.
fn widen(sql: &str) -> String {
    format!("CAST({sql} AS bigint)")
}

/// One function call's SQL per dialect.
fn render_call(function: &ValueFunction, args: &[String], dialect: Dialect) -> String {
    let joined = args.join(", ");
    // Postgres's `extract` returns `numeric`; the cast keeps `fn.day(d) / 2`
    // an integer division, as it is on SQLite, and at 64 bits.
    let date_part = |pg_field: &str, sqlite_format: &str| match dialect {
        Dialect::Postgres => widen(&format!("extract({pg_field} from {joined})")),
        Dialect::Sqlite => format!("CAST(strftime('{sqlite_format}', {joined}) AS integer)"),
    };
    match (function.name, dialect) {
        ("least", Dialect::Sqlite) => format!("min({joined})"),
        ("greatest", Dialect::Sqlite) => format!("max({joined})"),
        ("length", Dialect::Postgres) => widen(&format!("length({joined})")),
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
pub fn render_generated_clause(
    expr: &ValueExpr,
    dialect: Dialect,
    columns: &[SchemaColumn],
) -> Result<String, String> {
    Ok(format!(
        "GENERATED ALWAYS AS ({}) STORED",
        render_value_expr(expr, dialect, columns)?
    ))
}

/// The prefix of the SQLite guard's constraint name; the column follows it.
const SQLITE_GUARD_PREFIX: &str = "_ferro_range_";

/// The name of the SQLite range guard on generated column `column`.
pub fn sqlite_guard_name(column: &str) -> String {
    format!("{SQLITE_GUARD_PREFIX}{column}")
}

/// The generated column a SQLite range guard named `constraint` belongs to.
pub fn sqlite_guard_column(constraint: &str) -> Option<&str> {
    constraint.strip_prefix(SQLITE_GUARD_PREFIX)
}

/// The SQLite range guard of a generated column: one named column check, or
/// `None` when nothing in the expression can overflow.
///
/// Postgres raises when a value does not fit: the column (`smallint out of
/// range` at 2^15, `integer out of range` at 2^31) or any step of the
/// computation (`bigint out of range` at 2^63, `value out of range:
/// overflow` past the largest double). SQLite turns an integer overflow into
/// a `real` and a float overflow into `inf`, and a later step can bring
/// either back (`min(<real>, 28)` is `28`). So the check holds the column to
/// its Postgres range and recomputes every arithmetic step under the column,
/// each held to its own: an integer step must still be an integer, a float
/// step finite. The same row then fails on both. `NULL` passes, as it does
/// for every check.
///
/// `storage` is the column's Postgres storage type and `columns` the table's
/// columns, from which each step's type is read.
///
/// # Errors
/// As [`render_value_expr`].
pub fn render_sqlite_generated_guard(
    column: &str,
    storage: CanonicalType,
    expr: &ValueExpr,
    columns: &[SchemaColumn],
) -> Result<Option<String>, String> {
    let quoted = quote_ident(column);
    let mut terms: Vec<String> = Vec::new();
    match storage {
        CanonicalType::SmallInt => {
            terms.push(integer_range(&quoted, i16::MIN.into(), i16::MAX.into()))
        }
        CanonicalType::Integer => {
            terms.push(integer_range(&quoted, i32::MIN.into(), i32::MAX.into()))
        }
        CanonicalType::BigInt => terms.push(integer_term(&quoted)),
        CanonicalType::Double => terms.push(finite_term(&quoted)),
        _ => {}
    }
    let mut steps = Vec::new();
    collect_steps(expr, true, &mut steps);
    for step in steps {
        let rendered = render_value_expr(step, Dialect::Sqlite, columns)?;
        terms.push(if is_integer_valued(step, columns) {
            integer_term(&rendered)
        } else {
            finite_term(&rendered)
        });
    }
    Ok((!terms.is_empty()).then(|| {
        format!(
            "CONSTRAINT {} CHECK ({})",
            quote_ident(&sqlite_guard_name(column)),
            terms.join(" AND ")
        )
    }))
}

fn integer_term(sql: &str) -> String {
    format!("typeof({sql}) IN ('integer', 'null')")
}

fn integer_range(sql: &str, low: i64, high: i64) -> String {
    format!("{} AND {sql} BETWEEN {low} AND {high}", integer_term(sql))
}

fn finite_term(sql: &str) -> String {
    format!("abs({sql}) <= {:e}", f64::MAX)
}

/// Every arithmetic step (`+ - * /`, unary `-`) under the root, in order; the
/// root itself is the column, which the storage term holds.
fn collect_steps<'a>(expr: &'a ValueExpr, root: bool, out: &mut Vec<&'a ValueExpr>) {
    match expr {
        ValueExpr::Column { .. } | ValueExpr::Literal { .. } => {}
        ValueExpr::Binary { left, right, .. } => {
            if !root {
                out.push(expr);
            }
            collect_steps(left, false, out);
            collect_steps(right, false, out);
        }
        ValueExpr::Negate { operand } => {
            if !root {
                out.push(expr);
            }
            collect_steps(operand, false, out);
        }
        ValueExpr::Func { args, .. } => args.iter().for_each(|arg| collect_steps(arg, false, out)),
    }
}

/// Whether a step computes an integer (its operands are all integers), as
/// both databases type it; any float operand makes it a float.
fn is_integer_valued(expr: &ValueExpr, columns: &[SchemaColumn]) -> bool {
    match expr {
        ValueExpr::Column { name } => columns.iter().any(|col| {
            col.name == *name
                && matches!(
                    resolve_column_storage(col, Dialect::Postgres),
                    Ok(ResolvedStorage::Scalar(
                        CanonicalType::SmallInt | CanonicalType::Integer | CanonicalType::BigInt
                    ))
                )
        }),
        ValueExpr::Literal { token } => token.parse::<i64>().is_ok(),
        ValueExpr::Binary { left, right, .. } => {
            is_integer_valued(left, columns) && is_integer_valued(right, columns)
        }
        ValueExpr::Negate { operand } => is_integer_valued(operand, columns),
        ValueExpr::Func { name, args } => match name.as_str() {
            "length" | "year" | "month" | "day" => true,
            _ => args.iter().all(|arg| is_integer_valued(arg, columns)),
        },
    }
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
                    render_value_expr(&expr, dialect, &[]).unwrap(),
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
            assert_eq!(render_value_expr(&expr, dialect, &[]).unwrap(), "(- -5)");
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
                render_generated_clause(&expr, dialect, &[]).unwrap(),
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
        render_value_expr(&call(name, args), dialect, &[]).unwrap()
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
        assert_eq!(
            renders("length", &["name"], Dialect::Sqlite),
            "length(\"name\")"
        );
        assert_eq!(
            renders("length", &["name"], Dialect::Postgres),
            "CAST(length(\"name\") AS bigint)"
        );
    }

    #[test]
    fn year_renders_strftime_on_sqlite_and_an_integer_extract_on_postgres() {
        assert_eq!(
            renders("year", &["d"], Dialect::Sqlite),
            "CAST(strftime('%Y', \"d\") AS integer)"
        );
        assert_eq!(
            renders("year", &["d"], Dialect::Postgres),
            "CAST(extract(year from \"d\") AS bigint)"
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
            "CAST(extract(month from \"d\") AS bigint)"
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
            "CAST(extract(day from \"d\") AS bigint)"
        );
    }

    #[test]
    fn the_epic_day_class_nests_on_both_dialects() {
        let expr = ValueExpr::Func {
            name: "least".to_string(),
            args: vec![call("day", &["date"]), *literal("28")],
        };
        assert_eq!(
            render_value_expr(&expr, Dialect::Sqlite, &[]).unwrap(),
            "min(CAST(strftime('%d', \"date\") AS integer), 28)"
        );
        assert_eq!(
            render_value_expr(&expr, Dialect::Postgres, &[]).unwrap(),
            "least(CAST(extract(day from \"date\") AS bigint), 28)"
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

    fn schema_column(name: &str, logical: &str, db_type: Option<&str>) -> SchemaColumn {
        serde_json::from_value(serde_json::json!({
            "name": name,
            "logical_type": logical,
            "db_type": db_type,
            "db_type_explicit": db_type.is_some(),
            "nullable": false, "primary_key": false, "autoincrement": false,
            "unique": false, "index": false, "default": null, "format": null,
        }))
        .unwrap()
    }

    #[test]
    fn postgres_widens_narrow_integer_columns_to_bigint_and_sqlite_does_not() {
        let columns = [
            schema_column("a", "integer", None),
            schema_column("s", "integer", Some("smallint")),
            schema_column("big", "integer", Some("bigint")),
            schema_column("x", "number", None),
        ];
        let expr = ValueExpr::Binary {
            op: ValueBinaryOp::Add,
            left: Box::new(ValueExpr::Binary {
                op: ValueBinaryOp::Sub,
                left: column("a"),
                right: column("s"),
            }),
            right: Box::new(ValueExpr::Binary {
                op: ValueBinaryOp::Add,
                left: column("big"),
                right: column("x"),
            }),
        };
        assert_eq!(
            render_value_expr(&expr, Dialect::Postgres, &columns).unwrap(),
            "((CAST(\"a\" AS bigint) - CAST(\"s\" AS bigint)) + (\"big\" + \"x\"))"
        );
        assert_eq!(
            render_value_expr(&expr, Dialect::Sqlite, &columns).unwrap(),
            "((\"a\" - \"s\") + (\"big\" + \"x\"))"
        );
    }

    fn guard(column: &str, storage: CanonicalType, expr: &ValueExpr) -> Option<String> {
        let columns = [
            schema_column("a", "integer", None),
            schema_column("b", "integer", Some("bigint")),
            schema_column("x", "number", None),
        ];
        render_sqlite_generated_guard(column, storage, expr, &columns).unwrap()
    }

    fn binary(op: ValueBinaryOp, left: Box<ValueExpr>, right: Box<ValueExpr>) -> ValueExpr {
        ValueExpr::Binary { op, left, right }
    }

    #[test]
    fn the_sqlite_guard_holds_the_column_to_its_postgres_range() {
        let a = *column("a");
        assert_eq!(
            guard("n", CanonicalType::SmallInt, &a).unwrap(),
            "CONSTRAINT \"_ferro_range_n\" CHECK (typeof(\"n\") IN ('integer', 'null') AND \"n\" BETWEEN -32768 AND 32767)"
        );
        assert_eq!(
            guard("n", CanonicalType::Integer, &a).unwrap(),
            "CONSTRAINT \"_ferro_range_n\" CHECK (typeof(\"n\") IN ('integer', 'null') AND \"n\" BETWEEN -2147483648 AND 2147483647)"
        );
        assert_eq!(
            guard("n", CanonicalType::BigInt, &a).unwrap(),
            "CONSTRAINT \"_ferro_range_n\" CHECK (typeof(\"n\") IN ('integer', 'null'))"
        );
        assert_eq!(
            guard("x", CanonicalType::Double, &a).unwrap(),
            "CONSTRAINT \"_ferro_range_x\" CHECK (abs(\"x\") <= 1.7976931348623157e308)"
        );
        assert_eq!(guard("t", CanonicalType::Text, &a), None);
        assert_eq!(guard("f", CanonicalType::Boolean, &a), None);
    }

    #[test]
    fn the_sqlite_guard_recomputes_every_arithmetic_step_under_a_function() {
        // least(a * b, 28): an integer step under the column.
        let least = ValueExpr::Func {
            name: "least".to_string(),
            args: vec![
                binary(ValueBinaryOp::Mul, column("a"), column("b")),
                *literal("28"),
            ],
        };
        assert_eq!(
            guard("n", CanonicalType::BigInt, &least).unwrap(),
            "CONSTRAINT \"_ferro_range_n\" CHECK (typeof(\"n\") IN ('integer', 'null') AND typeof((\"a\" * \"b\")) IN ('integer', 'null'))"
        );
        // greatest(x + x, -x): float steps are held finite.
        let greatest = ValueExpr::Func {
            name: "greatest".to_string(),
            args: vec![
                binary(ValueBinaryOp::Add, column("x"), column("x")),
                ValueExpr::Negate {
                    operand: column("x"),
                },
            ],
        };
        assert_eq!(
            guard("y", CanonicalType::Double, &greatest).unwrap(),
            "CONSTRAINT \"_ferro_range_y\" CHECK (abs(\"y\") <= 1.7976931348623157e308 AND abs((\"x\" + \"x\")) <= 1.7976931348623157e308 AND abs((- \"x\")) <= 1.7976931348623157e308)"
        );
        // A nested step and the length function are integer steps; the root is the column.
        let nested = binary(
            ValueBinaryOp::Sub,
            Box::new(binary(ValueBinaryOp::Add, column("a"), literal("1"))),
            Box::new(call("length", &["s"])),
        );
        assert_eq!(
            guard("n", CanonicalType::BigInt, &nested).unwrap(),
            "CONSTRAINT \"_ferro_range_n\" CHECK (typeof(\"n\") IN ('integer', 'null') AND typeof((\"a\" + 1)) IN ('integer', 'null'))"
        );
    }

    #[test]
    fn the_guard_name_carries_its_column_back() {
        assert_eq!(
            sqlite_guard_column(&sqlite_guard_name("to\"tal")),
            Some("to\"tal")
        );
        assert_eq!(sqlite_guard_column("ck_ledger_total"), None);
    }

    #[test]
    fn an_unknown_function_is_an_error_naming_it() {
        let expr = ValueExpr::Func {
            name: "nope".to_string(),
            args: vec![],
        };
        let err = render_value_expr(&expr, Dialect::Postgres, &[]).unwrap_err();
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
