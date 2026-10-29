//! Value expressions: the function table and the one renderer (ADR-0059).
//!
//! A generated column's body is a [`ValueExpr`] over the row's own columns,
//! literals, `+ - * /`, unary `-` and the functions in [`VALUE_FUNCTIONS`].
//! The table is the single decision of which functions exist (I-1): Python
//! reads it over FFI to build `ferro.fn`, and [`render_value_expr`] renders
//! every call per dialect.

use std::collections::BTreeSet;

use ferro_schema_ir::{ValueBinaryOp, ValueExpr};
use serde::{Deserialize, Serialize};

use ferro_schema_ir::SchemaColumn;

use crate::{CanonicalType, Dialect, ResolvedStorage, quote_ident, resolve_column_storage};

/// The value families a function admits and returns.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
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

impl ValueType {
    const ALL: [ValueType; 7] = [
        ValueType::Integer,
        ValueType::Number,
        ValueType::Decimal,
        ValueType::String,
        ValueType::Date,
        ValueType::Datetime,
        ValueType::Boolean,
    ];

    /// The family's token, as the IR's `logical_type` and Python name it.
    pub fn token(self) -> &'static str {
        match self {
            ValueType::Integer => "integer",
            ValueType::Number => "number",
            ValueType::Decimal => "decimal",
            ValueType::String => "string",
            ValueType::Date => "date",
            ValueType::Datetime => "datetime",
            ValueType::Boolean => "boolean",
        }
    }

    /// The family named by a column's `logical_type`, if it is one.
    pub fn from_token(token: &str) -> Option<ValueType> {
        Self::ALL.into_iter().find(|family| family.token() == token)
    }

    /// The Python annotation the family is declared with.
    pub fn annotation(self) -> &'static str {
        match self {
            ValueType::Integer => "int",
            ValueType::Number => "float",
            ValueType::Decimal => "Decimal",
            ValueType::String => "str",
            ValueType::Date => "date",
            ValueType::Datetime => "datetime",
            ValueType::Boolean => "bool",
        }
    }

    /// Whether a value of this family fits a parameter of `parameter`: the
    /// same family, or an integer where a float or decimal is taken.
    pub fn fits(self, parameter: ValueType) -> bool {
        self == parameter
            || (self == ValueType::Integer
                && matches!(parameter, ValueType::Number | ValueType::Decimal))
    }
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
    /// The call on SQLite; `{args}` stands for the comma-joined arguments.
    #[serde(skip)]
    pub sqlite: &'static str,
    /// The call on Postgres, likewise.
    #[serde(skip)]
    pub postgres: &'static str,
    /// Whether Postgres returns an integer result at another type than
    /// `bigint` (`length` is `integer`, `extract` is `numeric`,
    /// `sign(bigint)` is `double precision`). The renderer then casts the
    /// call to `bigint`, the type SQLite computes the result at, so `fn.day(d)
    /// / 2` divides as integers on both.
    #[serde(skip)]
    pub postgres_narrows_integer: bool,
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
        sqlite: "sign({args})",
        postgres: "sign({args})",
        postgres_narrows_integer: true,
    },
    ValueFunction {
        name: "abs",
        signatures: NUMERIC_UNARY,
        nulls: NullRule::Propagate,
        sqlite: "abs({args})",
        postgres: "abs({args})",
        postgres_narrows_integer: false,
    },
    ValueFunction {
        name: "least",
        signatures: ORDERED_REPEATED,
        nulls: NullRule::Refused,
        sqlite: "min({args})",
        postgres: "least({args})",
        postgres_narrows_integer: false,
    },
    ValueFunction {
        name: "greatest",
        signatures: ORDERED_REPEATED,
        nulls: NullRule::Refused,
        sqlite: "max({args})",
        postgres: "greatest({args})",
        postgres_narrows_integer: false,
    },
    ValueFunction {
        name: "coalesce",
        sqlite: "coalesce({args})",
        postgres: "coalesce({args})",
        postgres_narrows_integer: false,
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
        sqlite: "length({args})",
        postgres: "length({args})",
        postgres_narrows_integer: true,
    },
    ValueFunction {
        name: "year",
        signatures: DATE_PART,
        nulls: NullRule::Propagate,
        sqlite: "CAST(strftime('%Y', {args}) AS integer)",
        postgres: "extract(year from {args})",
        postgres_narrows_integer: true,
    },
    ValueFunction {
        name: "month",
        signatures: DATE_PART,
        nulls: NullRule::Propagate,
        sqlite: "CAST(strftime('%m', {args}) AS integer)",
        postgres: "extract(month from {args})",
        postgres_narrows_integer: true,
    },
    ValueFunction {
        name: "day",
        signatures: DATE_PART,
        nulls: NullRule::Propagate,
        sqlite: "CAST(strftime('%d', {args}) AS integer)",
        postgres: "extract(day from {args})",
        postgres_narrows_integer: true,
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
/// On Postgres every value computes at the type SQLite computes it at, the
/// type [`type_value_expr`] gives it: an integer at 64 bits, a float as a
/// double. Postgres computes integer arithmetic at its operands' width and
/// SQLite at 64 bits, so an integer value narrower than `bigint` (a
/// `smallint` or `integer` column, a call [`ValueFunction::postgres_narrows_integer`]
/// marks) is cast to `bigint`. Postgres types a bare float literal (`0.1`)
/// as an exact `numeric`, so it is cast to `double precision`. An integer
/// literal stays bare: [`type_value_expr`] refuses a call over literals
/// only, so it always meets a `bigint` operand, which carries the step to
/// 64 bits. Then only the column's own range decides, as Postgres's
/// assignment to the column and the SQLite storage guard enforce.
///
/// # Errors
/// A message naming the function when a call names one the table lacks, or
/// the typing's refusal ([`type_value_expr`]) of an expression it must type.
pub fn render_value_expr(
    expr: &ValueExpr,
    dialect: Dialect,
    columns: &[SchemaColumn],
) -> Result<String, String> {
    let render = |expr: &ValueExpr| render_value_expr(expr, dialect, columns);
    let postgres = dialect == Dialect::Postgres;
    Ok(match expr {
        ValueExpr::Column { name } => {
            let quoted = quote_ident(name);
            let narrow = postgres
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
        ValueExpr::Literal { token } => match literal_type(token)? {
            ValueType::Number if postgres => format!("CAST({token} AS double precision)"),
            _ => token.clone(),
        },
        ValueExpr::Binary { op, left, right } => format!(
            "({} {} {})",
            render(left)?,
            binary_op_sql(*op),
            render(right)?
        ),
        ValueExpr::Negate { operand } => format!("(- {})", render(operand)?),
        ValueExpr::Func { name, args } => {
            let function = known_function(name)?;
            let rendered = args.iter().map(render).collect::<Result<Vec<_>, _>>()?;
            let template = if postgres {
                function.postgres
            } else {
                function.sqlite
            };
            let call = template.replace("{args}", &rendered.join(", "));
            if postgres
                && function.postgres_narrows_integer
                && call_result(function, expr, columns)? == ValueType::Integer
            {
                widen(&call)
            } else {
                call
            }
        }
    })
}

/// The table entry named `name`, or a refusal listing the table.
fn known_function(name: &str) -> Result<&'static ValueFunction, String> {
    value_function(name).ok_or_else(|| {
        let known: Vec<&str> = VALUE_FUNCTIONS.iter().map(|f| f.name).collect();
        format!(
            "unknown value function '{name}'; the functions are: {}",
            known.join(", ")
        )
    })
}

/// The result type of a call: the one every signature returns, else the
/// typing's answer for its arguments.
fn call_result(
    function: &ValueFunction,
    call: &ValueExpr,
    columns: &[SchemaColumn],
) -> Result<ValueType, String> {
    let first = function.signatures[0].result;
    if function.signatures.iter().all(|sig| sig.result == first) {
        return Ok(first);
    }
    Ok(type_value_expr(call, &|name| schema_value_column(columns, name))?.family)
}

/// A Postgres integer value at 64 bits.
fn widen(sql: &str) -> String {
    format!("CAST({sql} AS bigint)")
}

/// A column as a value expression reads it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
pub struct ValueColumn {
    /// The value family it holds; `None` for a column no expression reads
    /// (an enum, a JSON or binary column).
    pub family: Option<ValueType>,
    /// Whether it can hold `NULL`.
    pub nullable: bool,
    /// Whether it is retired, leaving the table.
    #[serde(default)]
    pub retired: bool,
}

/// The type of a value expression under value parity.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ValueTyping {
    /// The family both databases compute the value in.
    pub family: ValueType,
    /// Whether the value can be `NULL`.
    pub nullable: bool,
}

/// How the table's column `name` reads in a value expression.
pub fn schema_value_column(columns: &[SchemaColumn], name: &str) -> Option<ValueColumn> {
    columns
        .iter()
        .find(|col| col.name == name)
        .map(value_column)
}

/// How one column reads in a value expression: the one decision of its
/// value family, read by the class-definition checks and every renderer.
/// An enum column has none, whatever its storage.
pub fn value_column(col: &SchemaColumn) -> ValueColumn {
    let is_enum =
        col.enum_values.is_some() || col.enum_type_name.is_some() || col.postgres_native_enum;
    ValueColumn {
        family: if is_enum {
            None
        } else {
            ValueType::from_token(&col.logical_type)
        },
        nullable: col.nullable,
        retired: col.retired,
    }
}

/// Every value family with the Python annotation it is declared with, in
/// table order.
pub fn value_family_annotations() -> Vec<(&'static str, &'static str)> {
    ValueType::ALL
        .iter()
        .map(|family| (family.token(), family.annotation()))
        .collect()
}

/// The type of a literal token as both databases read it: `true` / `false`
/// a boolean, a quoted token a string, digits that fit 64 bits an integer,
/// any other number a float.
///
/// # Errors
/// An integer past 64 bits: SQLite reads it as a float and Postgres as an
/// exact `numeric`, so the two would compute different values.
pub fn literal_type(token: &str) -> Result<ValueType, String> {
    if token == "true" || token == "false" {
        return Ok(ValueType::Boolean);
    }
    if token.starts_with('\'') {
        return Ok(ValueType::String);
    }
    let digits = token.strip_prefix('-').unwrap_or(token);
    if !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()) {
        return token
            .parse::<i64>()
            .map(|_| ValueType::Integer)
            .map_err(|_| {
                format!(
                    "holds the integer literal {token}, outside the 64-bit range: SQLite \
                 computes it as a float and Postgres as an exact numeric, so the \
                 stored values would differ; write it as a float literal ({token}.0) \
                 or keep it within -9223372036854775808..9223372036854775807"
                )
            });
    }
    Ok(ValueType::Number)
}

fn quoted_name(name: &str) -> String {
    format!("'{name}'")
}

/// Type a value expression under value parity (ADR-0059): its family and
/// whether it can be `NULL`, from the columns `column` describes, the
/// literal rule ([`literal_type`]) and each call's signature in
/// [`VALUE_FUNCTIONS`]. Every door that needs an expression's type reads
/// this one walk: the class-definition refusals in Python over FFI, the
/// Postgres renderer and the SQLite guard.
///
/// # Errors
/// A sentence naming the fix when the expression could store a different
/// value, or fail differently, on SQLite and Postgres.
pub fn type_value_expr(
    expr: &ValueExpr,
    column: &dyn Fn(&str) -> Option<ValueColumn>,
) -> Result<ValueTyping, String> {
    let typed = |family, nullable| Ok(ValueTyping { family, nullable });
    match expr {
        ValueExpr::Column { name } => {
            let Some(col) = column(name) else {
                return Err(format!(
                    "reads {}, which is not a column of the model",
                    quoted_name(name)
                ));
            };
            if col.retired {
                return Err(format!(
                    "reads the retired column {}, which is leaving the table; read a \
                     column the model still maps",
                    quoted_name(name)
                ));
            }
            let Some(family) = col.family else {
                let annotations: Vec<&str> = ValueType::ALL
                    .iter()
                    .map(|family| family.annotation())
                    .collect();
                return Err(format!(
                    "reads {}, a column no value expression can read; it takes {} columns",
                    quoted_name(name),
                    annotations.join(", ")
                ));
            };
            typed(family, col.nullable)
        }
        ValueExpr::Literal { token } => typed(literal_type(token)?, false),
        ValueExpr::Negate { operand } => {
            let operand = type_value_expr(operand, column)?;
            if !matches!(
                operand.family,
                ValueType::Integer | ValueType::Number | ValueType::Decimal
            ) {
                return Err(format!(
                    "unary - takes a number, not {}",
                    operand.family.token()
                ));
            }
            Ok(operand)
        }
        ValueExpr::Binary { op, left, right } => {
            let symbol = binary_op_sql(*op);
            if *op == ValueBinaryOp::Div && !is_nonzero_number_literal(right) {
                return Err(
                    "divides by something other than a non-zero literal: Postgres raises \
                     on division by zero where SQLite stores NULL; divide by a non-zero \
                     literal such as / 100"
                        .to_string(),
                );
            }
            let left = type_value_expr(left, column)?;
            let right = type_value_expr(right, column)?;
            arithmetic_type(symbol, left, right)
        }
        ValueExpr::Func { name, args } => {
            let function = known_function(name)?;
            if args
                .iter()
                .all(|arg| matches!(arg, ValueExpr::Literal { .. }))
            {
                return Err(format!(
                    "calls fn.{name} over literals only, a constant that reads no \
                     column: compute it in Python and write the result as a literal, or \
                     call fn.{name} on a column"
                ));
            }
            let typed_args = args
                .iter()
                .map(|arg| type_value_expr(arg, column))
                .collect::<Result<Vec<_>, _>>()?;
            call_type(function, args, &typed_args)
        }
    }
}

fn is_nonzero_number_literal(expr: &ValueExpr) -> bool {
    let ValueExpr::Literal { token } = expr else {
        return false;
    };
    matches!(
        literal_type(token),
        Ok(ValueType::Integer | ValueType::Number)
    ) && token.parse::<f64>().is_ok_and(|value| value != 0.0)
}

fn arithmetic_type(
    symbol: &str,
    left: ValueTyping,
    right: ValueTyping,
) -> Result<ValueTyping, String> {
    let families = [left.family, right.family];
    if families.contains(&ValueType::Decimal) {
        return Err(format!(
            "'{symbol}' over a decimal: SQLite computes decimal arithmetic in floating \
             point and Postgres exactly, so the stored values would differ; store the \
             amount in minor units as an int"
        ));
    }
    if !families
        .iter()
        .all(|family| matches!(family, ValueType::Integer | ValueType::Number))
    {
        let mut shown: Vec<&str> = families.iter().map(|family| family.token()).collect();
        shown.sort_unstable();
        shown.dedup();
        return Err(format!(
            "'{symbol}' takes integers and floats, not {}",
            shown.join(", ")
        ));
    }
    let family = if families == [ValueType::Integer; 2] {
        ValueType::Integer
    } else {
        ValueType::Number
    };
    if family == ValueType::Number && matches!(symbol, "*" | "/") {
        return Err(format!(
            "'{symbol}' over a float: Postgres raises when a float product or quotient \
             underflows to zero where SQLite stores 0.0, so the databases would \
             disagree; keep the arithmetic in integers (an amount in minor units, such \
             as amount_minor) or compute the value in the application"
        ));
    }
    Ok(ValueTyping {
        family,
        nullable: left.nullable || right.nullable,
    })
}

fn signature_text(signature: &ValueSignature) -> String {
    let mut args: Vec<&str> = signature.args.iter().map(|arg| arg.token()).collect();
    if signature.variadic {
        args.push("...");
    }
    format!("({}) -> {}", args.join(", "), signature.result.token())
}

/// The family and nullability of one call, from the first signature its
/// arguments fit, and the function's null rule.
fn call_type(
    function: &ValueFunction,
    args: &[ValueExpr],
    typed: &[ValueTyping],
) -> Result<ValueTyping, String> {
    let families: Vec<ValueType> = typed.iter().map(|arg| arg.family).collect();
    let fits = |signature: &ValueSignature| {
        let params = signature.args;
        let arity_fits = if signature.variadic {
            families.len() >= params.len()
        } else {
            families.len() == params.len()
        };
        arity_fits
            && families.iter().enumerate().all(|(i, family)| {
                family.fits(*params.get(i).unwrap_or_else(|| &params[params.len() - 1]))
            })
    };
    let Some(signature) = function.signatures.iter().find(|sig| fits(sig)) else {
        let name = function.name;
        let reason = if families.contains(&ValueType::Datetime)
            && function
                .signatures
                .iter()
                .any(|sig| sig.args.contains(&ValueType::Date))
        {
            ": the part of a datetime depends on the session's time zone, which \
             Postgres refuses in a generated column; read a date field instead"
        } else if families.contains(&ValueType::String) && matches!(name, "least" | "greatest") {
            ": text compares by collation on Postgres and byte by byte on SQLite, so it \
             is refused"
        } else {
            ""
        };
        let signatures: Vec<String> = function.signatures.iter().map(signature_text).collect();
        let shown: Vec<&str> = families.iter().map(|family| family.token()).collect();
        return Err(format!(
            "fn.{name} takes {}, not ({}){reason}",
            signatures.join(", "),
            shown.join(", ")
        ));
    };
    let nullable = match function.nulls {
        NullRule::Refused => {
            if let Some(i) = typed.iter().position(|arg| arg.nullable) {
                let subject = match &args[i] {
                    ValueExpr::Column { name } => {
                        format!("the nullable column {}", quoted_name(name))
                    }
                    _ => "a nullable argument".to_string(),
                };
                return Err(format!(
                    "fn.{} over {subject}: Postgres skips a NULL where SQLite returns \
                     NULL; wrap it as fn.coalesce(<column>, <fallback>)",
                    function.name
                ));
            }
            false
        }
        NullRule::FirstNonNull => typed.iter().all(|arg| arg.nullable),
        NullRule::Propagate => typed.iter().any(|arg| arg.nullable),
    };
    Ok(ValueTyping {
        family: signature.result,
        nullable,
    })
}

/// The refusal of a generated column whose Postgres storage would hold its
/// value differently from SQLite, or `None` when it holds it alike.
///
/// A bounded `varchar(N)` is refused: Postgres raises on a longer value,
/// unless its excess is spaces, which it trims to fit, and SQLite stores
/// every character. No SQLite check reproduces the trim, so no guard can
/// hold the two to one value.
pub fn generated_storage_refusal(storage: CanonicalType) -> Option<String> {
    match storage {
        CanonicalType::Varchar(Some(length)) => Some(format!(
            "stores into varchar({length}): Postgres raises on a longer value (and \
             trims trailing spaces to fit) where SQLite stores every character; drop \
             db_type= so the column is text"
        )),
        _ => None,
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
    Ok(render_stored_generated_clause(&render_value_expr(
        expr, dialect, columns,
    )?))
}

/// `GENERATED ALWAYS AS (<body>) STORED` for an expression already
/// rendered: a declaration's, or the text a live catalog printed for a
/// column put back as it was.
pub fn render_stored_generated_clause(body: &str) -> String {
    format!("GENERATED ALWAYS AS ({body}) STORED")
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
        _ => {}
    }
    // A float column may hold `inf` (Postgres stores one an input carried),
    // so only its arithmetic is held, the root step included.
    let leave_out_root = storage != CanonicalType::Double;
    terms.extend(step_terms(expr, leave_out_root, columns)?);
    Ok((!terms.is_empty()).then(|| {
        format!(
            "CONSTRAINT {} CHECK ({})",
            quote_ident(&sqlite_guard_name(column)),
            terms.join(" AND ")
        )
    }))
}

/// One SQLite term per arithmetic step of `expr`, the root included: an
/// integer step must still be an integer, a float step finite. Each is
/// false on a row where Postgres raises computing that step, so a check
/// ANDing them fails the row SQLite would otherwise judge on a real or
/// `inf`.
///
/// # Errors
/// As [`render_value_expr`].
pub fn sqlite_step_terms(
    expr: &ValueExpr,
    columns: &[SchemaColumn],
) -> Result<Vec<String>, String> {
    step_terms(expr, false, columns)
}

/// The step terms under `expr`; `root` leaves out the root itself, which a
/// generated column's storage term holds.
fn step_terms(
    expr: &ValueExpr,
    root: bool,
    columns: &[SchemaColumn],
) -> Result<Vec<String>, String> {
    let mut steps = Vec::new();
    collect_steps(expr, root, &mut steps);
    let lookup = |name: &str| schema_value_column(columns, name);
    let mut terms = Vec::new();
    for step in steps {
        let rendered = render_value_expr(step, Dialect::Sqlite, columns)?;
        if type_value_expr(step, &lookup)?.family == ValueType::Integer {
            terms.push(integer_term(&rendered));
            continue;
        }
        // Negating a float never overflows.
        let ValueExpr::Binary { left, right, .. } = step else {
            continue;
        };
        // Postgres raises on a float step only when it overflows from finite
        // operands; an operand already infinite (a stored `inf`) passes.
        let mut alternatives = vec![finite_term(&rendered)];
        for operand in [left, right] {
            if matches!(**operand, ValueExpr::Literal { .. })
                || type_value_expr(operand, &lookup)?.family == ValueType::Integer
            {
                continue;
            }
            let term = infinite_term(&render_value_expr(operand, Dialect::Sqlite, columns)?);
            if !alternatives.contains(&term) {
                alternatives.push(term);
            }
        }
        terms.push(if alternatives.len() == 1 {
            alternatives.remove(0)
        } else {
            format!("({})", alternatives.join(" OR "))
        });
    }
    Ok(terms)
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

fn infinite_term(sql: &str) -> String {
    format!("abs({sql}) > {:e}", f64::MAX)
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

/// Every column the expression reads.
pub fn value_expr_columns(expr: &ValueExpr) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    collect_columns(expr, &mut out);
    out
}

/// Every column a table check's body reads: the column each leaf names,
/// a comparison's other column, and every column either operand of a
/// value-expression comparison reads.
pub fn check_expr_columns(expr: &ferro_schema_ir::CheckExpr) -> BTreeSet<String> {
    use ferro_schema_ir::{CheckExpr, CheckOperand};
    let mut out = BTreeSet::new();
    let mut stack = vec![expr];
    while let Some(expr) = stack.pop() {
        match expr {
            CheckExpr::And { left, right } | CheckExpr::Or { left, right } => {
                stack.push(left);
                stack.push(right);
            }
            CheckExpr::Not { child } => stack.push(child),
            CheckExpr::Cmp { column, other, .. } => {
                out.insert(column.clone());
                if let CheckOperand::Column { name } = other {
                    out.insert(name.clone());
                }
            }
            CheckExpr::Compare { left, right, .. } => {
                collect_columns(left, &mut out);
                collect_columns(right, &mut out);
            }
            CheckExpr::IsNull { column }
            | CheckExpr::IsNotNull { column }
            | CheckExpr::In { column, .. }
            | CheckExpr::Like { column, .. } => {
                out.insert(column.clone());
            }
        }
    }
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
            assert_eq!(
                render_value_expr(&expr, Dialect::Sqlite, &[]).unwrap(),
                format!("(\"a\" {symbol} 2)")
            );
            assert_eq!(
                render_value_expr(&expr, Dialect::Postgres, &[]).unwrap(),
                format!("(\"a\" {symbol} 2)")
            );
        }
    }

    #[test]
    fn unary_minus_never_forms_a_comment() {
        let expr = ValueExpr::Negate {
            operand: literal("-5"),
        };
        assert_eq!(
            render_value_expr(&expr, Dialect::Sqlite, &[]).unwrap(),
            "(- -5)"
        );
        assert_eq!(
            render_value_expr(&expr, Dialect::Postgres, &[]).unwrap(),
            "(- -5)"
        );
    }

    #[test]
    fn sign_renders_with_its_clause_on_both_dialects() {
        let expr = ValueExpr::Func {
            name: "sign".to_string(),
            args: vec![*column("amount_minor")],
        };
        let columns = [schema_column("amount_minor", "number", None)];
        for dialect in DIALECTS {
            assert_eq!(
                render_generated_clause(&expr, dialect, &columns).unwrap(),
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
    fn sign_of_an_integer_is_a_bigint_on_postgres_and_of_a_float_a_double() {
        // Postgres's sign(bigint) is a double precision; SQLite's is an integer.
        let columns = [
            schema_column("a", "integer", Some("bigint")),
            schema_column("x", "number", None),
        ];
        let render = |arg: &str, dialect| {
            render_value_expr(&call("sign", &[arg]), dialect, &columns).unwrap()
        };
        assert_eq!(render("a", Dialect::Sqlite), "sign(\"a\")");
        assert_eq!(
            render("a", Dialect::Postgres),
            "CAST(sign(\"a\") AS bigint)"
        );
        assert_eq!(render("x", Dialect::Sqlite), "sign(\"x\")");
        assert_eq!(render("x", Dialect::Postgres), "sign(\"x\")");
    }

    #[test]
    fn a_number_literal_renders_at_the_type_sqlite_computes_it_at_on_postgres() {
        let sum = |token: &str| ValueExpr::Binary {
            op: ValueBinaryOp::Add,
            left: column("a"),
            right: literal(token),
        };
        let columns = [schema_column("a", "integer", Some("bigint"))];
        let pg = |token| render_value_expr(&sum(token), Dialect::Postgres, &columns).unwrap();
        assert_eq!(pg("0.1"), "(\"a\" + CAST(0.1 AS double precision))");
        assert_eq!(pg("1e+20"), "(\"a\" + CAST(1e+20 AS double precision))");
        // An integer literal meets a bigint operand, which carries the step.
        assert_eq!(pg("7"), "(\"a\" + 7)");
        assert_eq!(
            render_value_expr(&sum("0.1"), Dialect::Sqlite, &columns).unwrap(),
            "(\"a\" + 0.1)"
        );
        for token in ["'x'", "true"] {
            assert_eq!(
                render_value_expr(&literal(token), Dialect::Postgres, &[]).unwrap(),
                token
            );
        }
    }

    #[test]
    fn the_literal_rule_refuses_an_integer_past_64_bits() {
        assert_eq!(literal_type("9223372036854775807"), Ok(ValueType::Integer));
        assert_eq!(literal_type("-9223372036854775808"), Ok(ValueType::Integer));
        assert_eq!(literal_type("0.5"), Ok(ValueType::Number));
        let err = literal_type("10000000000000000000").unwrap_err();
        assert!(
            err.contains("64-bit") && err.contains("10000000000000000000.0"),
            "{err}"
        );
    }

    fn facts(name: &str) -> Option<ValueColumn> {
        let family = match name {
            "a" => ValueType::Integer,
            "x" => ValueType::Number,
            "s" => ValueType::String,
            "d" => ValueType::Date,
            _ => return None,
        };
        Some(ValueColumn {
            family: Some(family),
            nullable: name == "s",
            retired: false,
        })
    }

    #[test]
    fn the_typing_reads_each_call_result_from_its_signature() {
        let typed = |expr: &ValueExpr| type_value_expr(expr, &facts).unwrap();
        assert_eq!(
            typed(&call("length", &["s"])),
            ValueTyping {
                family: ValueType::Integer,
                nullable: true
            }
        );
        assert_eq!(typed(&call("sign", &["x"])).family, ValueType::Number);
        assert_eq!(typed(&call("day", &["d"])).family, ValueType::Integer);
        // An integer argument fits a float signature only when no integer one does.
        assert_eq!(typed(&call("abs", &["a"])).family, ValueType::Integer);
        let mixed = ValueExpr::Binary {
            op: ValueBinaryOp::Add,
            left: column("a"),
            right: column("x"),
        };
        assert_eq!(typed(&mixed).family, ValueType::Number);
    }

    #[test]
    fn the_typing_refuses_with_the_fix_named() {
        let refused = |expr: &ValueExpr| type_value_expr(expr, &facts).unwrap_err();
        let least = ValueExpr::Func {
            name: "least".to_string(),
            args: vec![*column("s"), *column("s")],
        };
        assert!(refused(&least).contains("collation"), "{}", refused(&least));
        let product = ValueExpr::Binary {
            op: ValueBinaryOp::Mul,
            left: column("x"),
            right: column("x"),
        };
        assert!(refused(&product).contains("minor units"));
        let by_column = ValueExpr::Binary {
            op: ValueBinaryOp::Div,
            left: column("a"),
            right: column("a"),
        };
        assert!(refused(&by_column).contains("non-zero literal"));
        assert!(refused(&column("nope")).contains("'nope'"));
        let constant = ValueExpr::Func {
            name: "abs".to_string(),
            args: vec![*literal("-5")],
        };
        assert!(
            refused(&constant).contains("literals only"),
            "{}",
            refused(&constant)
        );
    }

    #[test]
    fn a_column_family_comes_from_its_logical_type_and_an_enum_has_none() {
        let int = schema_column("a", "integer", None);
        assert_eq!(value_column(&int).family, Some(ValueType::Integer));
        let mut labelled = schema_column("e", "string", None);
        labelled.enum_type_name = Some("status".to_string());
        assert_eq!(value_column(&labelled).family, None);
        assert_eq!(value_column(&schema_column("j", "json", None)).family, None);
        assert_eq!(value_family_annotations()[1], ("number", "float"));
    }

    #[test]
    fn a_bounded_varchar_is_refused_as_generated_storage() {
        let err = generated_storage_refusal(CanonicalType::Varchar(Some(5))).unwrap();
        assert!(
            err.contains("varchar(5)") && err.contains("drop db_type="),
            "{err}"
        );
        assert_eq!(generated_storage_refusal(CanonicalType::Text), None);
        assert_eq!(
            generated_storage_refusal(CanonicalType::Varchar(None)),
            None
        );
        assert_eq!(generated_storage_refusal(CanonicalType::BigInt), None);
    }

    #[test]
    fn every_function_renders_its_arguments_on_both_dialects() {
        for function in VALUE_FUNCTIONS {
            assert!(function.sqlite.contains("{args}"), "{}", function.name);
            assert!(function.postgres.contains("{args}"), "{}", function.name);
        }
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
        // A float column may hold an `inf` its input carried, as Postgres
        // stores it; with no arithmetic there is nothing to hold.
        assert_eq!(guard("x", CanonicalType::Double, &a), None);
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
        // greatest(x + x, -x): a float sum is held finite unless an operand
        // is already infinite; a negation never overflows.
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
            "CONSTRAINT \"_ferro_range_y\" CHECK ((abs((\"x\" + \"x\")) <= 1.7976931348623157e308 OR abs(\"x\") > 1.7976931348623157e308))"
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
