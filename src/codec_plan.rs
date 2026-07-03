//! Per-model compiled codec plan (FF-C C1).
//!
//! Every per-column type decision is made **once per model per schema epoch**
//! (at registration) instead of per value per row. Both facets of a column
//! come from the FF-B derived-type decision table
//! (`ferro_ddl_lowering::resolve_column_storage` / `canonical_from_parts`):
//!
//! - the **storage** token (`db_type`) — what the DDL emitters create, the
//!   codec↔DDL consistency witness;
//! - the **logical** codec (`ColumnCodec`) — how Python values bind, decode,
//!   and project. An explicit `db_type` may legally widen storage away from
//!   the logical family (e.g. `UUID` stored as `text`); the logical codec is
//!   what preserves the Python-facing value shape in that case.
//!
//! The `(logical, db_type)` pair is exactly the vocabulary of
//! [`CodecIrPayload`]'s `bind_rules` / `fetch_rules`; [`runtime_codec_ir_payload`]
//! is the runtime producer pinned by the golden vector
//! `tests/fixtures/ir_vectors/codec_registry_core_v1.json`.

use crate::schema::{property_json_type_and_format, resolve_column_storage_json};
use crate::state::Dialect;
use ferro_ddl_lowering::{
    CanonicalType, ResolvedStorage, canonical_from_parts, canonical_to_db_type_token,
    enum_label_strings,
};
use ferro_schema_ir::{CodecBindRule, CodecFetchRule, CodecIrPayload, HydrationAbi};
use std::collections::HashMap;

/// Storage decision for an enum-typed column.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EnumStorage {
    /// Native Postgres enum UDT (`CREATE TYPE ... AS ENUM`).
    PgNative { type_name: String },
    /// Text-family scalar storage (StrEnum, or explicit text/varchar override).
    Text,
    /// Integer-family scalar storage (IntEnum with explicit int-family `db_type`).
    Int,
}

/// One authoritative per-column codec: how values bind, decode, and project.
///
/// Variants mirror the FF-C roadmap enum and map 1:1 onto [`CodecIrPayload`]
/// rules via [`ColumnCodec::bind_rule`] / [`ColumnCodec::fetch_rule`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ColumnCodec {
    Int,
    SmallInt,
    BigInt,
    Float,
    Bool,
    Str,
    Bytes,
    Uuid,
    DateTime,
    Date,
    Time,
    Decimal,
    Json,
    Enum {
        values: Vec<String>,
        storage: EnumStorage,
        /// Enum member values are integers (`IntEnum`) — drives integer-typed
        /// binds and typed NULLs, mirroring the schema's `type: "integer"`.
        int_valued: bool,
    },
}

/// A column's compiled codec plus its canonical FF-B storage token.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ColumnCodecEntry {
    pub codec: ColumnCodec,
    /// Canonical `db_type` token from `resolve_column_storage` (the native
    /// enum's type name for `PgEnum` columns).
    pub db_type: String,
}

/// Compiled codec plan for one registered model.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ModelCodecPlan {
    columns: HashMap<String, ColumnCodecEntry>,
}

impl ModelCodecPlan {
    /// O(1) codec lookup; `None` for columns outside the model (joined/m2m).
    pub fn codec(&self, col_name: &str) -> Option<&ColumnCodec> {
        self.columns.get(col_name).map(|entry| &entry.codec)
    }

    /// Full entry lookup (codec + storage token). The storage token has no
    /// runtime consumer yet in C1 — it is the codec↔DDL consistency witness
    /// pinned by tests; C2/C3 consume it.
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn entry(&self, col_name: &str) -> Option<&ColumnCodecEntry> {
        self.columns.get(col_name)
    }

    /// Compile a model's enriched Pydantic JSON schema into a codec plan.
    ///
    /// Storage resolves through `resolve_column_storage_json` at the Postgres
    /// dialect — the dialect that preserves the most type information
    /// (Boolean, Uuid, native enums). The plan itself is dialect-agnostic;
    /// dialect-specific behavior (SQLite bool-as-int, etc.) stays a
    /// consumption-time branch. When storage and logical type agree on the
    /// type family, storage refines width (`int` + `db_type="bigint"` →
    /// `BigInt`); when an explicit `db_type` crosses families (UUID stored as
    /// text), the logical codec wins so Python value shapes are preserved.
    pub fn compile(schema: &serde_json::Value) -> ModelCodecPlan {
        let mut columns = HashMap::new();
        if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
            for (col_name, raw_col) in properties {
                let resolved = resolve_ref(schema, raw_col);
                let storage =
                    resolve_column_storage_json(col_name, raw_col, resolved, Dialect::Postgres);
                columns.insert(
                    col_name.clone(),
                    compile_column(raw_col, resolved, &storage),
                );
            }
        }
        ModelCodecPlan { columns }
    }
}

fn resolve_ref<'a>(
    schema: &'a serde_json::Value,
    col_info: &'a serde_json::Value,
) -> &'a serde_json::Value {
    if let Some(ref_path) = col_info.get("$ref").and_then(|r| r.as_str())
        && let Some(def_name) = ref_path.strip_prefix("#/$defs/")
        && let Some(def) = schema.get("$defs").and_then(|defs| defs.get(def_name))
    {
        return def;
    }
    col_info
}

fn compile_column(
    raw_col: &serde_json::Value,
    resolved: &serde_json::Value,
    storage: &ResolvedStorage,
) -> ColumnCodecEntry {
    let db_type = match storage {
        ResolvedStorage::Scalar(canonical) => {
            canonical_to_db_type_token(*canonical, Dialect::Postgres)
        }
        ResolvedStorage::PgEnum { type_name, .. } => type_name.clone(),
    };

    let (json_type, format) = property_json_type_and_format(resolved);

    let enum_values = resolved
        .get("enum")
        .or_else(|| raw_col.get("enum"))
        .and_then(|v| v.as_array())
        .filter(|v| !v.is_empty());
    if let Some(values) = enum_values {
        let enum_storage = match storage {
            ResolvedStorage::PgEnum { type_name, .. } => EnumStorage::PgNative {
                type_name: type_name.clone(),
            },
            ResolvedStorage::Scalar(canonical) => match type_family(*canonical) {
                TypeFamily::Int => EnumStorage::Int,
                _ => EnumStorage::Text,
            },
        };
        return ColumnCodecEntry {
            codec: ColumnCodec::Enum {
                values: enum_label_strings(values),
                storage: enum_storage,
                int_valued: json_type == Some("integer"),
            },
            db_type,
        };
    }
    // The same FF-B cascade WITHOUT the explicit-db_type override: the
    // logical side of the column (unknown types fall back to Varchar → Str,
    // matching `canonical_column_type`).
    let logical = canonical_from_parts(json_type.unwrap_or(""), format, "", Dialect::Postgres)
        .unwrap_or(CanonicalType::Varchar(None));
    let canonical = match storage {
        ResolvedStorage::Scalar(c) if type_family(*c) == type_family(logical) => *c,
        _ => logical,
    };
    ColumnCodecEntry {
        codec: scalar_codec(canonical),
        db_type,
    }
}

#[derive(PartialEq, Eq)]
enum TypeFamily {
    Int,
    Float,
    Decimal,
    Bool,
    Str,
    Bytes,
    Uuid,
    DateTime,
    Date,
    Time,
    Json,
}

fn type_family(canonical: CanonicalType) -> TypeFamily {
    match canonical {
        CanonicalType::Integer | CanonicalType::SmallInt | CanonicalType::BigInt => TypeFamily::Int,
        CanonicalType::Double => TypeFamily::Float,
        CanonicalType::Decimal => TypeFamily::Decimal,
        CanonicalType::Boolean => TypeFamily::Bool,
        CanonicalType::Text | CanonicalType::Varchar(_) | CanonicalType::Char(_) => TypeFamily::Str,
        CanonicalType::Blob => TypeFamily::Bytes,
        CanonicalType::Uuid => TypeFamily::Uuid,
        CanonicalType::DateTime | CanonicalType::Timestamp | CanonicalType::TimestampTz => {
            TypeFamily::DateTime
        }
        CanonicalType::Date => TypeFamily::Date,
        CanonicalType::Time => TypeFamily::Time,
        CanonicalType::Json => TypeFamily::Json,
    }
}

fn scalar_codec(canonical: CanonicalType) -> ColumnCodec {
    match canonical {
        CanonicalType::Integer => ColumnCodec::Int,
        CanonicalType::SmallInt => ColumnCodec::SmallInt,
        CanonicalType::BigInt => ColumnCodec::BigInt,
        CanonicalType::Double => ColumnCodec::Float,
        CanonicalType::Decimal => ColumnCodec::Decimal,
        CanonicalType::Boolean => ColumnCodec::Bool,
        CanonicalType::Text | CanonicalType::Varchar(_) | CanonicalType::Char(_) => {
            ColumnCodec::Str
        }
        CanonicalType::Blob => ColumnCodec::Bytes,
        CanonicalType::Uuid => ColumnCodec::Uuid,
        CanonicalType::DateTime | CanonicalType::Timestamp | CanonicalType::TimestampTz => {
            ColumnCodec::DateTime
        }
        CanonicalType::Date => ColumnCodec::Date,
        CanonicalType::Time => ColumnCodec::Time,
        CanonicalType::Json => ColumnCodec::Json,
    }
}

impl ColumnCodec {
    /// The `logical_type` token in CodecIR vocabulary.
    pub fn logical_token(&self) -> &'static str {
        match self {
            ColumnCodec::Int | ColumnCodec::SmallInt | ColumnCodec::BigInt => "integer",
            ColumnCodec::Float => "number",
            ColumnCodec::Bool => "boolean",
            ColumnCodec::Str => "string",
            ColumnCodec::Bytes => "binary",
            ColumnCodec::Uuid => "uuid",
            ColumnCodec::DateTime => "datetime",
            ColumnCodec::Date => "date",
            ColumnCodec::Time => "time",
            ColumnCodec::Decimal => "decimal",
            ColumnCodec::Json => "json",
            ColumnCodec::Enum { .. } => "enum",
        }
    }

    /// The canonical `db_type` token this codec stores as when no explicit
    /// override widens it (the CodecIR rule key).
    fn canonical_db_type(&self) -> &'static str {
        match self {
            ColumnCodec::Int => "int",
            ColumnCodec::SmallInt => "smallint",
            ColumnCodec::BigInt => "bigint",
            ColumnCodec::Float => "double",
            ColumnCodec::Bool => "boolean",
            ColumnCodec::Str => "text",
            ColumnCodec::Bytes => "bytea",
            ColumnCodec::Uuid => "uuid",
            ColumnCodec::DateTime => "timestamptz",
            ColumnCodec::Date => "date",
            ColumnCodec::Time => "time",
            ColumnCodec::Decimal => "numeric",
            ColumnCodec::Json => "json",
            ColumnCodec::Enum { .. } => "enum",
        }
    }

    /// `(non_null_wire_kind, null_wire_kind)` in CodecIR vocabulary.
    fn wire_kinds(&self) -> (&'static str, &'static str) {
        match self {
            ColumnCodec::Int | ColumnCodec::SmallInt | ColumnCodec::BigInt => ("i64", "i64_null"),
            ColumnCodec::Float => ("f64", "f64_null"),
            ColumnCodec::Bool => ("bool", "bool_null"),
            ColumnCodec::Str => ("string", "string_null"),
            ColumnCodec::Bytes => ("bytes", "bytes_null"),
            ColumnCodec::Uuid => ("uuid", "uuid_null"),
            ColumnCodec::DateTime => ("timestamp_text", "timestamp_null"),
            ColumnCodec::Date => ("date_text", "date_null"),
            ColumnCodec::Time => ("time_text", "time_null"),
            ColumnCodec::Decimal => ("numeric_text", "numeric_null"),
            ColumnCodec::Json => ("json_text", "json_null"),
            ColumnCodec::Enum { .. } => ("enum_text", "enum_null"),
        }
    }

    /// Python type name after hydration, in CodecIR vocabulary.
    fn python_kind(&self) -> &'static str {
        match self {
            ColumnCodec::Int | ColumnCodec::SmallInt | ColumnCodec::BigInt => "int",
            ColumnCodec::Float => "float",
            ColumnCodec::Bool => "bool",
            ColumnCodec::Str => "str",
            ColumnCodec::Bytes => "bytes",
            ColumnCodec::Uuid => "uuid.UUID",
            ColumnCodec::DateTime => "datetime.datetime",
            ColumnCodec::Date => "datetime.date",
            ColumnCodec::Time => "datetime.time",
            ColumnCodec::Decimal => "decimal.Decimal",
            ColumnCodec::Json => "object",
            ColumnCodec::Enum { .. } => "enum.Enum",
        }
    }

    /// This codec's bind rule for `db_type` (defaults to the canonical token).
    pub fn bind_rule(&self, db_type: Option<&str>) -> CodecBindRule {
        let (non_null, null) = self.wire_kinds();
        CodecBindRule {
            logical_type: self.logical_token().to_string(),
            db_type: db_type.unwrap_or(self.canonical_db_type()).to_string(),
            non_null_wire_kind: non_null.to_string(),
            null_wire_kind: null.to_string(),
        }
    }

    /// This codec's fetch rule for `db_type` (defaults to the canonical token).
    pub fn fetch_rule(&self, db_type: Option<&str>) -> CodecFetchRule {
        let (non_null, _) = self.wire_kinds();
        CodecFetchRule {
            db_type: db_type.unwrap_or(self.canonical_db_type()).to_string(),
            wire_kind: non_null.to_string(),
            python_kind: self.python_kind().to_string(),
        }
    }
}

/// The full runtime codec rule table as a [`CodecIrPayload`] — the runtime
/// consumer the CodecIR RFC promised. The golden vector
/// `codec_registry_core_v1.json` is asserted to be a byte-identical subset
/// (see `runtime_codec_ir_tests`), so the vectors pin the table that actually
/// drives bind/decode.
pub fn runtime_codec_ir_payload() -> CodecIrPayload {
    let placeholder_enum = ColumnCodec::Enum {
        values: Vec::new(),
        storage: EnumStorage::Text,
        int_valued: false,
    };
    let vocabulary: [(&ColumnCodec, Option<&str>); 15] = [
        (&ColumnCodec::Uuid, None),
        (&ColumnCodec::Int, Some("smallint")),
        (&ColumnCodec::Int, Some("int")),
        (&ColumnCodec::Int, Some("bigint")),
        (&ColumnCodec::Float, None),
        (&ColumnCodec::Bool, None),
        (&ColumnCodec::Str, None),
        (&ColumnCodec::Bytes, None),
        (&ColumnCodec::DateTime, Some("timestamp")),
        (&ColumnCodec::DateTime, Some("timestamptz")),
        (&ColumnCodec::Date, None),
        (&ColumnCodec::Time, None),
        (&ColumnCodec::Decimal, None),
        (&ColumnCodec::Json, None),
        (&placeholder_enum, None),
    ];
    CodecIrPayload {
        bind_rules: vocabulary
            .iter()
            .map(|(codec, db_type)| codec.bind_rule(*db_type))
            .collect(),
        fetch_rules: vocabulary
            .iter()
            .map(|(codec, db_type)| codec.fetch_rule(*db_type))
            .collect(),
        hydration_abi: HydrationAbi {
            constructor_mode: "direct_dict".to_string(),
            required_slots: vec![
                "__pydantic_fields_set__".to_string(),
                "__pydantic_extra__".to_string(),
                "__pydantic_private__".to_string(),
            ],
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ferro_schema_ir::IrEnvelope;
    use serde_json::json;

    /// The golden vector pins the RUNTIME rule table: every rule in
    /// `codec_registry_core_v1.json` must appear byte-identically in
    /// [`runtime_codec_ir_payload`] (which is a superset), and the hydration
    /// ABI must match exactly. This is the CodecIR runtime consumer the RFC
    /// promised — the vectors no longer only pin serde shape.
    #[test]
    fn golden_vector_rules_pin_the_runtime_table() {
        let fixture = include_str!("../tests/fixtures/ir_vectors/codec_registry_core_v1.json");
        let parsed: serde_json::Value = serde_json::from_str(fixture).unwrap();
        let envelope: IrEnvelope<CodecIrPayload> =
            serde_json::from_value(parsed.get("ir").cloned().unwrap()).unwrap();
        let runtime = runtime_codec_ir_payload();

        for rule in &envelope.payload.bind_rules {
            assert!(
                runtime.bind_rules.contains(rule),
                "golden bind rule missing from runtime table: {rule:?}"
            );
        }
        for rule in &envelope.payload.fetch_rules {
            assert!(
                runtime.fetch_rules.contains(rule),
                "golden fetch rule missing from runtime table: {rule:?}"
            );
        }
        assert_eq!(envelope.payload.hydration_abi, runtime.hydration_abi);
    }

    /// Every rule the runtime table emits uses the CodecIR vocabulary
    /// consistently: one bind rule and one fetch rule per `db_type` key.
    #[test]
    fn runtime_table_is_keyed_consistently() {
        let runtime = runtime_codec_ir_payload();
        assert_eq!(runtime.bind_rules.len(), runtime.fetch_rules.len());
        for (bind, fetch) in runtime.bind_rules.iter().zip(&runtime.fetch_rules) {
            assert_eq!(bind.db_type, fetch.db_type);
            assert_eq!(bind.non_null_wire_kind, fetch.wire_kind);
        }
    }

    fn plan_for(properties: serde_json::Value) -> ModelCodecPlan {
        ModelCodecPlan::compile(&json!({ "properties": properties }))
    }

    /// F5: a `pattern=` constraint must never change a column's codec.
    #[test]
    fn numeric_pattern_str_field_compiles_to_str() {
        let plan = plan_for(json!({
            "year_code": {"type": "string", "pattern": "^\\d{4}$"},
            "serial": {"anyOf": [
                {"type": "string", "pattern": "^\\d{6}$"},
                {"type": "null"}
            ]}
        }));
        assert_eq!(plan.codec("year_code"), Some(&ColumnCodec::Str));
        assert_eq!(plan.codec("serial"), Some(&ColumnCodec::Str));
        assert_eq!(plan.entry("year_code").unwrap().db_type, "varchar");
    }

    /// Real Decimal fields are recognized by the enriched `format: "decimal"`.
    #[test]
    fn decimal_format_compiles_to_decimal() {
        let plan = plan_for(json!({
            "amount": {
                "anyOf": [
                    {"type": "number"},
                    {"type": "string", "pattern": "^-?\\d+(\\.\\d+)?$"}
                ],
                "format": "decimal"
            }
        }));
        assert_eq!(plan.codec("amount"), Some(&ColumnCodec::Decimal));
        assert_eq!(plan.entry("amount").unwrap().db_type, "numeric");
    }

    /// An explicit `db_type` that widens away from the logical family keeps
    /// the logical codec (Python value shapes) while the storage token
    /// witnesses the DDL decision.
    #[test]
    fn uuid_stored_as_text_keeps_uuid_codec_with_text_storage() {
        let plan = plan_for(json!({
            "external_id": {"type": "string", "format": "uuid", "db_type": "text"}
        }));
        let entry = plan.entry("external_id").unwrap();
        assert_eq!(entry.codec, ColumnCodec::Uuid);
        assert_eq!(entry.db_type, "text");
    }

    /// Same-family explicit tokens refine width instead.
    #[test]
    fn same_family_db_type_refines_width() {
        let plan = plan_for(json!({
            "value": {"type": "integer", "db_type": "bigint"}
        }));
        let entry = plan.entry("value").unwrap();
        assert_eq!(entry.codec, ColumnCodec::BigInt);
        assert_eq!(entry.db_type, "bigint");
    }

    #[test]
    fn enum_columns_capture_values_storage_and_int_valuedness() {
        let plan = plan_for(json!({
            "status": {
                "enum": ["active", "archived"],
                "type": "string",
                "enum_type_name": "docstatus"
            },
            "priority": {"enum": [1, 2], "type": "integer", "db_type": "smallint"}
        }));
        assert_eq!(
            plan.codec("status"),
            Some(&ColumnCodec::Enum {
                values: vec!["active".to_string(), "archived".to_string()],
                storage: EnumStorage::PgNative {
                    type_name: "docstatus".to_string()
                },
                int_valued: false,
            })
        );
        assert_eq!(
            plan.codec("priority"),
            Some(&ColumnCodec::Enum {
                values: vec!["1".to_string(), "2".to_string()],
                storage: EnumStorage::Int,
                int_valued: true,
            })
        );
    }

    /// Scalar cascade parity with the DDL side for the derived families,
    /// including the temporal/uuid/json formats.
    #[test]
    fn derived_families_match_ddl_cascade() {
        let plan = plan_for(json!({
            "id": {"type": "integer", "primary_key": true},
            "name": {"type": "string"},
            "ratio": {"type": "number"},
            "active": {"type": "boolean"},
            "payload": {"type": "object"},
            "tags": {"type": "array"},
            "data": {"type": "string", "format": "binary"},
            "uid": {"type": "string", "format": "uuid"},
            "created": {"type": "string", "format": "date-time"},
            "day": {"type": "string", "format": "date"},
            "at": {"type": "string", "format": "time"}
        }));
        for (col, codec) in [
            ("id", ColumnCodec::Int),
            ("name", ColumnCodec::Str),
            ("ratio", ColumnCodec::Float),
            ("active", ColumnCodec::Bool),
            ("payload", ColumnCodec::Json),
            ("tags", ColumnCodec::Json),
            ("data", ColumnCodec::Bytes),
            ("uid", ColumnCodec::Uuid),
            ("created", ColumnCodec::DateTime),
            ("day", ColumnCodec::Date),
            ("at", ColumnCodec::Time),
        ] {
            assert_eq!(plan.codec(col), Some(&codec), "column {col}");
        }
    }

    /// The schema epoch: re-registering a model rebuilds its plan atomically.
    #[test]
    fn reregistration_rebuilds_plan() {
        let name = "EpochModel".to_string();
        let first = crate::state::RegisteredModel::new(json!({
            "properties": {"v": {"type": "integer"}}
        }));
        let second = crate::state::RegisteredModel::new(json!({
            "properties": {"v": {"type": "string"}}
        }));
        {
            let mut registry = crate::state::MODEL_REGISTRY.write().unwrap();
            registry.insert(name.clone(), first);
            assert_eq!(
                registry.get(&name).unwrap().codec_plan.codec("v"),
                Some(&ColumnCodec::Int)
            );
            registry.insert(name.clone(), second);
            assert_eq!(
                registry.get(&name).unwrap().codec_plan.codec("v"),
                Some(&ColumnCodec::Str)
            );
            registry.remove(&name);
        }
    }
}
