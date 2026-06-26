//! Versioned intermediate representation (IR) types for Ferro schema, query, and codec contracts.
//!
//! Python builds IR envelopes and passes them across the FFI boundary as JSON. Rust deserializes
//! into these types for query planning, shadow comparisons, and (eventually) full IR-first DDL.
//! Fixture round-trip tests in this crate pin wire stability for `tests/fixtures/ir_vectors/`.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Tagged envelope wrapping any Ferro IR payload.
///
/// Every IR document on the wire carries a kind and version so consumers can reject unknown
/// formats before interpreting `payload`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct IrEnvelope<T> {
    /// Discriminator for the payload shape (e.g. `"schema"`, `"query"`, `"codec"`).
    pub ir_kind: String,
    /// Monotonic schema version for `ir_kind`; bump when the payload contract changes.
    pub ir_version: u32,
    /// The deserialized IR body.
    pub payload: T,
}

/// Schema IR: registered models and their table-level artifacts.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaIrPayload {
    /// When true, column `db_type` tokens are backend-neutral until lowered by an emitter.
    pub dialect_agnostic: bool,
    /// One entry per registered model, in registration order.
    pub models: Vec<SchemaModel>,
}

/// One model's logical schema as emitted from Python.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaModel {
    /// PascalCase model class name (e.g. `"User"`).
    pub model_name: String,
    /// Physical table name (typically `model_name.to_lowercase()`).
    pub table_name: String,
    /// Column definitions in declaration order.
    pub columns: Vec<SchemaColumn>,
    /// Outgoing foreign keys from this table.
    pub foreign_keys: Vec<SchemaForeignKey>,
    /// Non-unique indexes (single- and multi-column).
    pub indexes: Vec<SchemaIndex>,
    /// Unique constraints (single- and multi-column).
    pub uniques: Vec<SchemaUnique>,
    /// Check constraints (`db_check=True` and similar).
    pub checks: Vec<SchemaCheck>,
}

/// One column in schema IR.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaColumn {
    /// SQL column name.
    pub name: String,
    /// Pydantic JSON Schema type family (e.g. `"string"`, `"integer"`).
    pub logical_type: String,
    /// Canonical storage token (`text`, `uuid`, `timestamptz`, …) after Ferro lowering.
    pub db_type: String,
    /// `true` when the user set an explicit `db_type=` on the field.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub db_type_explicit: Option<bool>,
    /// Whether the column allows SQL `NULL`.
    pub nullable: bool,
    /// Primary-key column flag.
    pub primary_key: bool,
    /// Autoincrement / serial semantics for integer PKs.
    pub autoincrement: bool,
    /// Single-column `unique=True`.
    pub unique: bool,
    /// Single-column `index=True`.
    pub index: bool,
    /// Server-side default as JSON (scalar literals only on the wire).
    pub default: Option<Value>,
    /// Pydantic `format` when present (`uuid`, `date-time`, …).
    pub format: Option<String>,
    /// Allowed enum literals when the field is a string enum.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub enum_values: Option<Vec<Value>>,
    /// Postgres native enum type name when applicable.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub enum_type_name: Option<String>,
    /// Live introspection only: column is a Postgres native enum UDT.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub postgres_native_enum: bool,
}

/// Foreign-key edge from `column` to `to_table.to_column`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaForeignKey {
    /// Local column (shadow `*_id` when using `ForeignKey`).
    pub column: String,
    /// Referenced table.
    pub to_table: String,
    /// Referenced column (usually the target PK).
    pub to_column: String,
    /// `ON DELETE` action name when set (`CASCADE`, `SET NULL`, …).
    pub on_delete: Option<String>,
    /// Explicit constraint name when provided.
    pub name: Option<String>,
}

/// B-tree index definition.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaIndex {
    /// Canonical index name (`idx_<table>_<col>` or composite variant).
    pub name: String,
    /// Indexed columns in order.
    pub columns: Vec<String>,
    /// `true` for `UNIQUE` indexes.
    pub unique: bool,
}

/// Unique constraint (may be backed by a unique index).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaUnique {
    /// Canonical unique name (`uq_<table>_<col>` or composite variant).
    pub name: String,
    /// Constrained columns in order.
    pub columns: Vec<String>,
}

/// `CHECK` constraint on one or more columns.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaCheck {
    /// Canonical check name (`ck_<table>_<col>` for single-column checks).
    pub name: String,
    /// SQL boolean expression inside the check.
    pub expression: String,
}

/// Query IR: filter, sort, pagination, and optional M2M join context.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QueryIrPayload {
    /// Model class name the query targets.
    pub model_name: String,
    /// Root-level predicate nodes (implicitly AND-combined by the planner).
    #[serde(rename = "where")]
    pub where_clause: Vec<QueryNode>,
    /// `ORDER BY` clauses in application order.
    pub order_by: Vec<QueryOrderBy>,
    /// `LIMIT` when set.
    pub limit: Option<u64>,
    /// `OFFSET` when set.
    pub offset: Option<u64>,
    /// Many-to-many join metadata JSON, deserialized into [`M2mContext`] downstream.
    pub m2m: Option<Value>,
}

/// One `ORDER BY` term.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QueryOrderBy {
    /// Column name or qualified identifier.
    pub column: String,
    /// Sort direction (`"asc"` or `"desc"`, case-insensitive in the planner).
    pub direction: String,
}

/// Predicate tree node in query IR (leaf comparison or compound AND/OR).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "node_kind")]
pub enum QueryNode {
    /// Column comparison (`==`, `!=`, `<`, `IN`, …) with a typed RHS.
    #[serde(rename = "leaf")]
    Leaf {
        /// Comparison or membership operator token.
        operator: String,
        /// Left-hand column name.
        column: String,
        /// Typed right-hand value.
        value: QueryValue,
    },
    /// Binary boolean combination of two child nodes.
    #[serde(rename = "compound")]
    Compound {
        /// `"AND"` or `"OR"`.
        operator: String,
        /// Left subtree.
        left: Box<QueryNode>,
        /// Right subtree.
        right: Box<QueryNode>,
    },
}

/// Typed literal or parameter value on the RHS of a leaf predicate.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QueryValue {
    /// Wire kind discriminator (`"null"`, `"string"`, `"int"`, …).
    pub kind: String,
    /// JSON-encoded value matching `kind`.
    pub value: Value,
}

/// Codec IR: bind/fetch rules and hydration ABI metadata for cross-language parity tests.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CodecIrPayload {
    /// Rules for encoding Python/Rust values into SQL bind parameters.
    pub bind_rules: Vec<CodecBindRule>,
    /// Rules for decoding wire values into Python-facing types.
    pub fetch_rules: Vec<CodecFetchRule>,
    /// How hydrated instances must initialize Pydantic slots (see AGENTS.md I-2).
    pub hydration_abi: HydrationAbi,
}

/// Maps a logical column type to wire and null bind kinds.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CodecBindRule {
    /// Pydantic JSON type family.
    pub logical_type: String,
    /// Canonical `db_type` token.
    pub db_type: String,
    /// Bind kind for non-null values.
    pub non_null_wire_kind: String,
    /// Bind kind for SQL `NULL` (typed null binds on Postgres).
    pub null_wire_kind: String,
}

/// Maps a stored SQL type to wire and Python kinds after fetch.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CodecFetchRule {
    /// Canonical or declared SQL type token.
    pub db_type: String,
    /// Kind on the Rust wire (`EngineValue` family).
    pub wire_kind: String,
    /// Python type name after hydration (`str`, `int`, `datetime`, …).
    pub python_kind: String,
}

/// Contract for direct-to-dict hydration without calling `BaseModel.__init__`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct HydrationAbi {
    /// Construction mode (`"direct_dict"` today).
    pub constructor_mode: String,
    /// Pydantic slot names that must be initialized on every hydrated instance.
    pub required_slots: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schema_fixture_roundtrip() {
        let fixture =
            include_str!("../../../tests/fixtures/ir_vectors/schema_invoice_baseline_v1.json");
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("schema fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<SchemaIrPayload> =
            serde_json::from_value(ir.clone()).expect("schema IR must deserialize");
        let encoded = serde_json::to_value(&envelope).expect("schema IR must serialize");
        assert_eq!(encoded, ir, "schema round-trip must not drift");
    }

    #[test]
    fn query_fixture_roundtrip() {
        let fixture =
            include_str!("../../../tests/fixtures/ir_vectors/query_user_compound_v1.json");
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("query fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> =
            serde_json::from_value(ir.clone()).expect("query IR must deserialize");
        let encoded = serde_json::to_value(&envelope).expect("query IR must serialize");
        assert_eq!(encoded, ir, "query round-trip must not drift");
    }

    #[test]
    fn codec_fixture_roundtrip() {
        let fixture =
            include_str!("../../../tests/fixtures/ir_vectors/codec_registry_core_v1.json");
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("codec fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<CodecIrPayload> =
            serde_json::from_value(ir.clone()).expect("codec IR must deserialize");
        let encoded = serde_json::to_value(&envelope).expect("codec IR must serialize");
        assert_eq!(encoded, ir, "codec round-trip must not drift");
    }
}
