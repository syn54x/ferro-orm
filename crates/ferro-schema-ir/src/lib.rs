use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct IrEnvelope<T> {
    pub ir_kind: String,
    pub ir_version: u32,
    pub payload: T,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaIrPayload {
    pub dialect_agnostic: bool,
    pub models: Vec<SchemaModel>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaModel {
    pub model_name: String,
    pub table_name: String,
    pub columns: Vec<SchemaColumn>,
    pub foreign_keys: Vec<SchemaForeignKey>,
    pub indexes: Vec<SchemaIndex>,
    pub uniques: Vec<SchemaUnique>,
    pub checks: Vec<SchemaCheck>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaColumn {
    pub name: String,
    pub logical_type: String,
    pub db_type: String,
    pub nullable: bool,
    pub primary_key: bool,
    pub autoincrement: bool,
    pub unique: bool,
    pub index: bool,
    pub default: Option<Value>,
    pub format: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaForeignKey {
    pub column: String,
    pub to_table: String,
    pub to_column: String,
    pub on_delete: Option<String>,
    pub name: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaIndex {
    pub name: String,
    pub columns: Vec<String>,
    pub unique: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaUnique {
    pub name: String,
    pub columns: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaCheck {
    pub name: String,
    pub expression: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QueryIrPayload {
    pub model_name: String,
    #[serde(rename = "where")]
    pub where_clause: Vec<QueryNode>,
    pub order_by: Vec<QueryOrderBy>,
    pub limit: Option<u64>,
    pub offset: Option<u64>,
    pub m2m: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QueryOrderBy {
    pub column: String,
    pub direction: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "node_kind")]
pub enum QueryNode {
    #[serde(rename = "leaf")]
    Leaf {
        operator: String,
        column: String,
        value: QueryValue,
    },
    #[serde(rename = "compound")]
    Compound {
        operator: String,
        left: Box<QueryNode>,
        right: Box<QueryNode>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QueryValue {
    pub kind: String,
    pub value: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CodecIrPayload {
    pub bind_rules: Vec<CodecBindRule>,
    pub fetch_rules: Vec<CodecFetchRule>,
    pub hydration_abi: HydrationAbi,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CodecBindRule {
    pub logical_type: String,
    pub db_type: String,
    pub non_null_wire_kind: String,
    pub null_wire_kind: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CodecFetchRule {
    pub db_type: String,
    pub wire_kind: String,
    pub python_kind: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct HydrationAbi {
    pub constructor_mode: String,
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
