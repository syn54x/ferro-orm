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
    /// Canonical storage token. Present only for columns with an explicit `db_type=`;
    /// `None` means "derive storage from `logical_type`/`format`".
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub db_type: Option<String>,
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

/// `CHECK` constraint on one column (enum `<col> IN (...)` shape).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchemaCheck {
    /// Canonical check name (`ck_<table>_<col>` for single-column checks).
    pub name: String,
    /// The constrained column (structured — no longer parsed out of a string).
    pub column: String,
    /// Pre-rendered SQL literal tokens for the allowed values, e.g. `["'admin'", "'user'"]`.
    pub values: Vec<String>,
}

/// Query IR: filter, sort, pagination, joins, materialization plan, and
/// optional M2M join context.
///
/// `ir_version: 7` (unconditional, no earlier version emitted anywhere —
/// #269, #278, #285, #292, #310, #314). `joins` is required and always
/// present (`[]` when the query traverses no relation); every leaf and
/// `order_by` entry carries a `path` (required, `[]` = root model);
/// `materialization` is required — the plan travels with the query as data
/// (ADR-0007), never inferred from a column list. v5 gave `record` fields
/// expression sources (#292, ADR-0009); v6 gave predicate trees the
/// recursive `not` node kind (uniform negation, #310, ADR-0008); v7 adds the
/// recursive `exists` node kind (existence tests, #314, ADR-0007).
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
    /// Relation joins collected from traversal (`[]` until #270 renders them).
    pub joins: Vec<QueryJoin>,
    /// What the query's result columns become (required in v3, ADR-0007).
    pub materialization: Materialization,
}

/// A query's materialization plan (ADR-0007): what its result columns become.
///
/// Every v4 query carries exactly one plan. `root_instances` is complete
/// root-model rows hydrated into model instances — the default and the only
/// plan the non-projecting walkers accept. `record` is partial selects: a
/// typed column subset decoded into projected records (#279). `instances` is
/// populated instance graphs (joined-row hydration, #284): the root rows plus
/// every included relation path's complete hop rows, hydrated into a
/// populated graph.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "kind")]
pub enum Materialization {
    /// Complete root-model instances — one full row per result (the default).
    #[serde(rename = "root_instances")]
    RootInstances,
    /// Projected records: a typed column subset decoded into `Row` records.
    #[serde(rename = "record")]
    Record {
        /// Projected fields in selection order.
        fields: Vec<RecordField>,
    },
    /// Populated instance graphs (joined-row hydration, #284/#286).
    #[serde(rename = "instances")]
    Instances {
        /// Included relation paths, each an ordered hop-fact list from the
        /// root model (the same hop shape as the `joins` section). Nothing
        /// else travels here: every hop of a populated graph is a complete
        /// row, permanently (the complete-instance invariant, ADR-0008), so
        /// there is no per-path configuration.
        paths: Vec<Vec<QueryJoinHop>>,
    },
}

/// One projected field in a [`Materialization::Record`] plan.
///
/// `name` is declared separately from the source, so output aliases are free
/// (`name` is the alias). A field reads from exactly one source (v5, #292,
/// ADR-0009): a `column` + relation `path` (plain and traversed projection),
/// or an `expr` (aggregations) — never both, never neither, enforced at
/// deserialization via [`RecordFieldWire`]. GROUP BY never travels on the
/// wire: the renderer derives group keys from the plan — every non-`expr`
/// field is a key (ADR-0009).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(try_from = "RecordFieldWire")]
pub struct RecordField {
    /// Output field name on the projected record.
    pub name: String,
    /// Source column the value reads from. `Some` iff `expr` is `None`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub column: Option<String>,
    /// Relation path from the root model to `column`'s table (`[]` = root
    /// model; non-empty paths are traversed projection, #293). Belongs to the
    /// `column` arm: an `expr` field carries its traversal on the expression
    /// itself, so its outer `path` is always `[]`.
    pub path: Vec<String>,
    /// Expression source instead of a column (aggregations, #294/#295).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expr: Option<RecordExpr>,
}

/// The expression source of an aggregate record field (v5, #292, ADR-0009).
///
/// Self-contained on the wire: `fn` travels as data (the closed aggregate
/// set), and `path` carries the source column's traversal as ordered hop
/// facts — the same hop shape as the `joins` section — rather than keying
/// into it.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RecordExpr {
    /// Aggregate function applied to `column`.
    pub r#fn: AggregateFn,
    /// Source column the aggregate reads from.
    pub column: String,
    /// Relation hops from the root model to `column`'s table (`[]` = root
    /// model), in the `joins`-section hop-fact shape.
    pub path: Vec<QueryJoinHop>,
}

/// The closed set of aggregate functions a [`RecordExpr`] may apply
/// (ADR-0009). Carried as data on the wire; an unknown token fails the strict
/// parse naming the supported set.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum AggregateFn {
    Count,
    Sum,
    Avg,
    Min,
    Max,
}

/// Field-wise wire shape backing [`RecordField`] deserialization, so the
/// exactly-one-source invariant is a parse-time rejection, not a downstream
/// walker check.
#[derive(Debug, Deserialize)]
struct RecordFieldWire {
    name: String,
    #[serde(default)]
    column: Option<String>,
    path: Vec<String>,
    #[serde(default)]
    expr: Option<RecordExpr>,
}

impl TryFrom<RecordFieldWire> for RecordField {
    type Error = String;

    fn try_from(wire: RecordFieldWire) -> Result<Self, Self::Error> {
        match (&wire.column, &wire.expr) {
            (Some(_), Some(_)) => Err(format!(
                "record field {:?} carries both `column` and `expr`; \
                 a field reads from exactly one source",
                wire.name
            )),
            (None, None) => Err(format!(
                "record field {:?} carries neither `column` nor `expr`; \
                 a field reads from exactly one source",
                wire.name
            )),
            (None, Some(_)) if !wire.path.is_empty() => Err(format!(
                "record field {:?} carries an `expr` and a non-empty field \
                 `path` {:?}; an expression field's traversal travels on \
                 `expr.path` (hop facts), so its field `path` must be empty",
                wire.name, wire.path
            )),
            _ => Ok(RecordField {
                name: wire.name,
                column: wire.column,
                path: wire.path,
                expr: wire.expr,
            }),
        }
    }
}

/// One relation JOIN collected from query-time traversal (#270 lands rendering).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QueryJoin {
    /// `"inner"` (default traversal semantics) or `"left"` (`.left_join`).
    pub join_type: String,
    /// Relation hops from the root model to the joined table, in traversal order.
    pub path: Vec<QueryJoinHop>,
}

/// One hop in a [`QueryJoin`] path: a single FK edge between two tables.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QueryJoinHop {
    /// Relation field name on the source side of this hop.
    pub relation: String,
    /// Local column the hop joins from (shadow `*_id` for `ForeignKey`).
    pub from_column: String,
    /// Table this hop joins into.
    pub to_table: String,
    /// Column on `to_table` the hop joins against (usually the target PK).
    pub to_column: String,
}

/// One `ORDER BY` term.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QueryOrderBy {
    /// Column name or qualified identifier.
    pub column: String,
    /// Sort direction (`"asc"` or `"desc"`, case-insensitive in the planner).
    pub direction: String,
    /// Relation field names from the root model to the ordered column's table;
    /// `[]` = root model (#269 requires this empty until #270 renders joins).
    pub path: Vec<String>,
}

/// Predicate tree node in query IR: leaf comparison, compound AND/OR, a
/// recursive NOT wrapping any child (uniform negation, v6, ADR-0008), or a
/// recursive existence test over a reverse/M2M relation (v7, ADR-0007).
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
        /// Relation field names from the root model to `column`'s table;
        /// `[]` = root model (#269 requires this empty until #270 renders joins).
        path: Vec<String>,
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
    /// Uniform negation (`~`, ADR-0008): SQL `NOT (...)` over the child.
    /// Fully general — the child may be any node kind, at any depth; there
    /// are no per-operator negative forms on the wire.
    #[serde(rename = "not")]
    Not {
        /// The negated subtree.
        child: Box<QueryNode>,
    },
    /// Existence test on a reverse or M2M relation (ADR-0007): a correlated
    /// `EXISTS (SELECT 1 ...)` at every cardinality. Carries no negation
    /// flag — NOT EXISTS is the `not` node over this one.
    #[serde(rename = "exists")]
    Exists {
        /// Correlation hop path in the `joins`-section hop-fact shape: one
        /// hop for a reverse FK (`FROM child WHERE child.fk = root.pk`), two
        /// for M2M (join table, then target). The first hop's table is the
        /// subquery FROM, correlated to the enclosing scope's alias;
        /// remaining hops render as inner joins inside the subquery.
        hops: Vec<QueryJoinHop>,
        /// Ordinary inner condition tree over the relation's target model
        /// (`[]` = bare test). May itself contain `exists`/`not` nodes —
        /// nesting is recursion, not a second mechanism.
        #[serde(rename = "where")]
        where_clause: Vec<QueryNode>,
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
            include_str!("../../../tests/fixtures/ir_vectors/query_user_compound_v7.json");
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("query fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> =
            serde_json::from_value(ir.clone()).expect("query IR must deserialize");
        // v3 (#278): the materialization plan travels with the query.
        assert_eq!(
            envelope.payload.materialization,
            Materialization::RootInstances
        );
        let encoded = serde_json::to_value(&envelope).expect("query IR must serialize");
        assert_eq!(encoded, ir, "query round-trip must not drift");
    }

    #[test]
    fn query_not_leaf_fixture_roundtrip() {
        // The not-over-leaf golden vector (#312, ADR-0008): uniform negation
        // travels as one recursive `not` node wrapping its child — here a
        // leaf `IN` comparison (the NOT IN spelling) — and must survive a
        // deserialize/serialize round-trip without drift.
        let fixture =
            include_str!("../../../tests/fixtures/ir_vectors/query_user_not_leaf_v7.json");
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("query not-leaf fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> =
            serde_json::from_value(ir.clone()).expect("query not-leaf IR must deserialize");
        // The wire carries a `not` node whose child is the IN leaf.
        match &envelope.payload.where_clause[0] {
            QueryNode::Not { child } => match child.as_ref() {
                QueryNode::Leaf { operator, .. } => assert_eq!(operator, "IN"),
                other => panic!("not child must be the IN leaf, got {other:?}"),
            },
            other => panic!("where[0] must be a not node, got {other:?}"),
        }
        let encoded = serde_json::to_value(&envelope).expect("query not-leaf IR must serialize");
        assert_eq!(encoded, ir, "query not-leaf round-trip must not drift");
    }

    #[test]
    fn query_not_compound_fixture_roundtrip() {
        // The not-over-compound golden vector (#313, ADR-0008): `~` wraps an
        // OR compound whole — no De Morgan expansion on the wire — and must
        // survive a deserialize/serialize round-trip without drift.
        let fixture =
            include_str!("../../../tests/fixtures/ir_vectors/query_user_not_compound_v7.json");
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("query not-compound fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> =
            serde_json::from_value(ir.clone()).expect("query not-compound IR must deserialize");
        match &envelope.payload.where_clause[0] {
            QueryNode::Not { child } => match child.as_ref() {
                QueryNode::Compound { operator, .. } => assert_eq!(operator, "OR"),
                other => panic!("not child must be the OR compound, got {other:?}"),
            },
            other => panic!("where[0] must be a not node, got {other:?}"),
        }
        let encoded =
            serde_json::to_value(&envelope).expect("query not-compound IR must serialize");
        assert_eq!(encoded, ir, "query not-compound round-trip must not drift");
    }

    #[test]
    fn query_exists_fixture_roundtrip() {
        // The bare 1-hop existence-test golden vector (#314, ADR-0007): an
        // `exists` node carries its correlation hop path plus an empty inner
        // condition tree, and must survive a deserialize/serialize
        // round-trip without drift.
        let fixture =
            include_str!("../../../tests/fixtures/ir_vectors/query_account_exists_v7.json");
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("query exists fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> =
            serde_json::from_value(ir.clone()).expect("query exists IR must deserialize");
        match &envelope.payload.where_clause[0] {
            QueryNode::Exists { hops, where_clause } => {
                assert_eq!(hops.len(), 1);
                assert_eq!(hops[0].relation, "transactions");
                assert_eq!(hops[0].from_column, "id");
                assert_eq!(hops[0].to_table, "transaction");
                assert_eq!(hops[0].to_column, "account_id");
                assert!(where_clause.is_empty(), "bare test carries no inner tree");
            }
            other => panic!("where[0] must be an exists node, got {other:?}"),
        }
        let encoded = serde_json::to_value(&envelope).expect("query exists IR must serialize");
        assert_eq!(encoded, ir, "query exists round-trip must not drift");
    }

    #[test]
    fn query_not_exists_fixture_roundtrip() {
        // NOT EXISTS on the wire is the ordinary `not` node over an `exists`
        // node — the exists node carries no negation flag (ADR-0008
        // composition; #314).
        let fixture =
            include_str!("../../../tests/fixtures/ir_vectors/query_owner_not_exists_v7.json");
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("query not-exists fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> =
            serde_json::from_value(ir.clone()).expect("query not-exists IR must deserialize");
        match &envelope.payload.where_clause[0] {
            QueryNode::Not { child } => match child.as_ref() {
                QueryNode::Exists { hops, .. } => assert_eq!(hops[0].relation, "accounts"),
                other => panic!("not child must be the exists node, got {other:?}"),
            },
            other => panic!("where[0] must be a not node, got {other:?}"),
        }
        let encoded = serde_json::to_value(&envelope).expect("query not-exists IR must serialize");
        assert_eq!(encoded, ir, "query not-exists round-trip must not drift");
    }

    #[test]
    fn query_traversal_fixture_roundtrip() {
        // Multi-hop `joins` section + path-carrying leaves must survive a
        // deserialize/serialize round-trip without drift (#270 wire stability).
        let fixture = include_str!(
            "../../../tests/fixtures/ir_vectors/query_transaction_traversal_v7.json"
        );
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("query traversal fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> =
            serde_json::from_value(ir.clone()).expect("query traversal IR must deserialize");
        // The joins section must actually carry the multi-hop path.
        assert_eq!(envelope.payload.joins.len(), 2);
        assert_eq!(envelope.payload.joins[1].path.len(), 2);
        // An order_by entry carrying a relation path (#271) survives round-trip too.
        assert_eq!(envelope.payload.order_by[0].path, vec!["account".to_string()]);
        let encoded = serde_json::to_value(&envelope).expect("query traversal IR must serialize");
        assert_eq!(encoded, ir, "query traversal round-trip must not drift");
    }

    #[test]
    fn query_left_join_fixture_roundtrip() {
        // A LEFT-marked path plus a deeper INNER entry sharing its prefix
        // (mixed LEFT-prefix/INNER-suffix, #272) must survive a
        // deserialize/serialize round-trip without drift, and the join_type
        // tokens must reach Rust exactly as written on the wire.
        let fixture =
            include_str!("../../../tests/fixtures/ir_vectors/query_transaction_left_join_v7.json");
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("query left_join fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> =
            serde_json::from_value(ir.clone()).expect("query left_join IR must deserialize");
        // The wire carries the mixed edge types: a "left" 1-hop prefix and an
        // "inner" 2-hop entry sharing it.
        assert_eq!(envelope.payload.joins.len(), 2);
        assert_eq!(envelope.payload.joins[0].join_type, "left");
        assert_eq!(envelope.payload.joins[0].path.len(), 1);
        assert_eq!(envelope.payload.joins[1].join_type, "inner");
        assert_eq!(envelope.payload.joins[1].path.len(), 2);
        let encoded = serde_json::to_value(&envelope).expect("query left_join IR must serialize");
        assert_eq!(encoded, ir, "query left_join round-trip must not drift");
    }

    #[test]
    fn query_record_fixture_roundtrip() {
        // The record-plan golden vector (#279): a projected query's full
        // payload — predicate, order, limit, and a two-field record plan —
        // survives a deserialize/serialize round-trip without drift.
        let fixture =
            include_str!("../../../tests/fixtures/ir_vectors/query_transaction_record_v7.json");
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("query record fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> =
            serde_json::from_value(ir.clone()).expect("query record IR must deserialize");
        let Materialization::Record { fields } = &envelope.payload.materialization else {
            panic!(
                "expected a record plan, got {:?}",
                envelope.payload.materialization
            );
        };
        assert_eq!(fields.len(), 2);
        assert_eq!(fields[0].name, "id");
        assert_eq!(fields[1].name, "amount");
        let encoded = serde_json::to_value(&envelope).expect("query record IR must serialize");
        assert_eq!(encoded, ir, "query record round-trip must not drift");
    }

    #[test]
    fn record_materialization_roundtrips() {
        // The `record` kind deserializes today (#278) even though the runtime
        // rejects it until #279 builds the record walker: the types are the
        // contract, the walkers are the implementation.
        let wire = serde_json::json!({
            "kind": "record",
            "fields": [
                {"name": "id", "column": "id", "path": []},
                {"name": "amount", "column": "amount", "path": []}
            ]
        });
        let plan: Materialization =
            serde_json::from_value(wire.clone()).expect("record kind must deserialize");
        let Materialization::Record { fields } = &plan else {
            panic!("expected Record, got {plan:?}");
        };
        assert_eq!(fields.len(), 2);
        assert_eq!(fields[0].name, "id");
        assert_eq!(fields[0].column.as_deref(), Some("id"));
        assert!(fields[0].path.is_empty());
        assert!(fields[0].expr.is_none(), "a column field carries no expr");
        let encoded = serde_json::to_value(&plan).expect("record kind must serialize");
        assert_eq!(encoded, wire, "record round-trip must not drift");
    }

    #[test]
    fn traversed_record_field_roundtrips() {
        // v5 (#292): a plain traversed record field — non-empty relation
        // `path`, a `column`, no `expr` — is the shape the tracer-bullet
        // slice (#293) consumes. The path stays relation names (it keys into
        // the `joins` section, like `order_by.path`), not hop facts.
        let wire = serde_json::json!({
            "kind": "record",
            "fields": [
                {"name": "account_name", "column": "name", "path": ["account"]},
                {"name": "owner_email", "column": "email",
                 "path": ["account", "owner"]}
            ]
        });
        let plan: Materialization =
            serde_json::from_value(wire.clone()).expect("traversed fields must deserialize");
        let Materialization::Record { fields } = &plan else {
            panic!("expected Record, got {plan:?}");
        };
        assert_eq!(fields[0].path, vec!["account".to_string()]);
        assert_eq!(fields[1].path.len(), 2);
        let encoded = serde_json::to_value(&plan).expect("traversed fields must serialize");
        assert_eq!(encoded, wire, "traversed record round-trip must not drift");
    }

    #[test]
    fn record_expr_fields_roundtrip() {
        // v5 (#292): expression fields — root and traversed sources. `fn`
        // travels as data; `expr.path` carries hop facts (the `joins`-section
        // shape), self-contained rather than keying into the joins list.
        let wire = serde_json::json!({
            "kind": "record",
            "fields": [
                {"name": "acct", "column": "account_id", "path": []},
                {"name": "total", "path": [],
                 "expr": {"fn": "sum", "column": "amount", "path": []}},
                {"name": "avg_balance", "path": [],
                 "expr": {"fn": "avg", "column": "balance", "path": [
                     {"relation": "account", "from_column": "account_id",
                      "to_table": "account", "to_column": "id"}
                 ]}}
            ]
        });
        let plan: Materialization =
            serde_json::from_value(wire.clone()).expect("expr fields must deserialize");
        let Materialization::Record { fields } = &plan else {
            panic!("expected Record, got {plan:?}");
        };
        assert_eq!(fields.len(), 3);
        // The group-key field is a plain column source.
        assert_eq!(fields[0].column.as_deref(), Some("account_id"));
        assert!(fields[0].expr.is_none());
        // The root aggregate: fn as data, no column, empty hop path.
        let total = fields[1].expr.as_ref().expect("total must carry an expr");
        assert!(fields[1].column.is_none());
        assert_eq!(total.r#fn, AggregateFn::Sum);
        assert_eq!(total.column, "amount");
        assert!(total.path.is_empty());
        // The traversed aggregate: hop facts on the expression itself.
        let avg = fields[2].expr.as_ref().expect("avg must carry an expr");
        assert_eq!(avg.r#fn, AggregateFn::Avg);
        assert_eq!(avg.path.len(), 1);
        assert_eq!(avg.path[0].relation, "account");
        assert_eq!(avg.path[0].from_column, "account_id");
        let encoded = serde_json::to_value(&plan).expect("expr fields must serialize");
        assert_eq!(encoded, wire, "expr record round-trip must not drift");
    }

    #[test]
    fn record_field_with_both_column_and_expr_is_rejected() {
        // Exactly-one-of is a deserialization invariant (#292): both sources
        // present fails the strict parse naming the field.
        let err = serde_json::from_value::<Materialization>(serde_json::json!({
            "kind": "record",
            "fields": [{
                "name": "total", "column": "amount", "path": [],
                "expr": {"fn": "sum", "column": "amount", "path": []}
            }]
        }))
        .expect_err("both column and expr must fail to deserialize");
        let msg = err.to_string();
        assert!(
            msg.contains("total") && msg.contains("both"),
            "must name the field and the violation: {msg}"
        );
    }

    #[test]
    fn record_field_with_neither_column_nor_expr_is_rejected() {
        let err = serde_json::from_value::<Materialization>(serde_json::json!({
            "kind": "record",
            "fields": [{"name": "total", "path": []}]
        }))
        .expect_err("neither column nor expr must fail to deserialize");
        let msg = err.to_string();
        assert!(
            msg.contains("total") && msg.contains("neither"),
            "must name the field and the violation: {msg}"
        );
    }

    #[test]
    fn record_expr_with_unknown_fn_is_rejected() {
        // `fn` is the closed aggregate set (ADR-0009); an unknown token fails
        // the strict parse naming the bad token and the supported set.
        let err = serde_json::from_value::<Materialization>(serde_json::json!({
            "kind": "record",
            "fields": [{
                "name": "mid", "path": [],
                "expr": {"fn": "median", "column": "amount", "path": []}
            }]
        }))
        .expect_err("an unknown aggregate fn must fail to deserialize");
        let msg = err.to_string();
        assert!(msg.contains("median"), "must name the bad fn: {msg}");
        for supported in ["count", "sum", "avg", "min", "max"] {
            assert!(
                msg.contains(supported),
                "must name supported fn {supported:?}: {msg}"
            );
        }
    }

    #[test]
    fn record_expr_field_with_outer_path_is_rejected() {
        // An expression field's traversal travels on `expr.path`; a non-empty
        // field-level `path` next to an `expr` is two contradictory traversal
        // claims and fails the strict parse.
        let err = serde_json::from_value::<Materialization>(serde_json::json!({
            "kind": "record",
            "fields": [{
                "name": "avg_balance", "path": ["account"],
                "expr": {"fn": "avg", "column": "balance", "path": []}
            }]
        }))
        .expect_err("an expr field with a field-level path must fail to deserialize");
        let msg = err.to_string();
        assert!(
            msg.contains("avg_balance") && msg.contains("expr.path"),
            "must name the field and point at expr.path: {msg}"
        );
    }

    #[test]
    fn query_global_aggregate_fixture_roundtrip() {
        // The global-aggregate golden vector (#294): an aggregate-only record
        // plan — count, root sum, and a traversed avg carrying hop facts on
        // the expression, plus a `joins` entry sharing the avg's path (join
        // identity) — survives a deserialize/serialize round-trip without
        // drift. No group keys: the whole result collapses to one record.
        let fixture = include_str!(
            "../../../tests/fixtures/ir_vectors/query_transaction_global_aggregate_v7.json"
        );
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("global aggregate fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> = serde_json::from_value(ir.clone())
            .expect("global aggregate IR must deserialize");
        let Materialization::Record { fields } = &envelope.payload.materialization else {
            panic!(
                "expected a record plan, got {:?}",
                envelope.payload.materialization
            );
        };
        assert_eq!(fields.len(), 3);
        assert!(
            fields.iter().all(|f| f.expr.is_some()),
            "a global aggregate plan is aggregate-only"
        );
        assert_eq!(
            fields[0].expr.as_ref().map(|e| e.r#fn),
            Some(AggregateFn::Count)
        );
        let encoded =
            serde_json::to_value(&envelope).expect("global aggregate IR must serialize");
        assert_eq!(encoded, ir, "global aggregate round-trip must not drift");
    }

    #[test]
    fn query_traversed_record_fixture_roundtrip() {
        // The traversed-projection golden vector (#293): an aliased root
        // field, a 1-hop field under a LEFT-marked path, and a 2-hop field
        // sharing that path's prefix — the full dict-selector wire shape,
        // joins section included — survives a deserialize/serialize
        // round-trip without drift.
        let fixture = include_str!(
            "../../../tests/fixtures/ir_vectors/query_transaction_traversed_record_v7.json"
        );
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("query traversed record fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> = serde_json::from_value(ir.clone())
            .expect("query traversed record IR must deserialize");
        let Materialization::Record { fields } = &envelope.payload.materialization else {
            panic!(
                "expected a record plan, got {:?}",
                envelope.payload.materialization
            );
        };
        assert_eq!(fields.len(), 3);
        // The root field is aliased: name and column differ.
        assert_eq!(fields[0].name, "txn_id");
        assert_eq!(fields[0].column.as_deref(), Some("id"));
        // Traversed fields carry relation-name paths keying into `joins`.
        assert_eq!(fields[1].path, vec!["account".to_string()]);
        assert_eq!(fields[2].path.len(), 2);
        assert_eq!(envelope.payload.joins[0].join_type, "left");
        let encoded =
            serde_json::to_value(&envelope).expect("query traversed record IR must serialize");
        assert_eq!(encoded, ir, "query traversed record round-trip must not drift");
    }

    #[test]
    fn query_aggregate_fixture_roundtrip() {
        // The aggregate-projection golden vector (#292): a grouped query's
        // full payload — a traversed group key sharing its path with the
        // `joins` section, a root aggregate, a traversed aggregate carrying
        // hop facts on the expression, and an output-name ORDER BY — survives
        // a deserialize/serialize round-trip without drift. GROUP BY does not
        // travel: the renderer derives it from the non-expr fields (ADR-0009).
        let fixture = include_str!(
            "../../../tests/fixtures/ir_vectors/query_transaction_aggregate_v7.json"
        );
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("query aggregate fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> =
            serde_json::from_value(ir.clone()).expect("query aggregate IR must deserialize");
        let Materialization::Record { fields } = &envelope.payload.materialization else {
            panic!(
                "expected a record plan, got {:?}",
                envelope.payload.materialization
            );
        };
        assert_eq!(fields.len(), 3);
        assert_eq!(fields[0].path, vec!["account".to_string()]);
        assert_eq!(
            fields[1].expr.as_ref().map(|e| e.r#fn),
            Some(AggregateFn::Sum)
        );
        assert_eq!(
            fields[2].expr.as_ref().map(|e| e.path.len()),
            Some(1),
            "the traversed aggregate carries its hop facts"
        );
        let encoded = serde_json::to_value(&envelope).expect("query aggregate IR must serialize");
        assert_eq!(encoded, ir, "query aggregate round-trip must not drift");
    }

    #[test]
    fn instances_materialization_roundtrips() {
        // v4 (#285): the `instances` kind carries `paths` — ordered hop-fact
        // lists in the same hop shape as the `joins` section — and nothing
        // else. The types are the contract; the walkers reject the kind until
        // the tracer bullet (#286) builds the fetch path.
        let wire = serde_json::json!({
            "kind": "instances",
            "paths": [
                [
                    {"relation": "account", "from_column": "account_id",
                     "to_table": "account", "to_column": "id"},
                    {"relation": "owner", "from_column": "owner_id",
                     "to_table": "owner", "to_column": "id"}
                ]
            ]
        });
        let plan: Materialization =
            serde_json::from_value(wire.clone()).expect("instances kind must deserialize");
        let Materialization::Instances { paths } = &plan else {
            panic!("expected Instances, got {plan:?}");
        };
        assert_eq!(paths.len(), 1);
        assert_eq!(paths[0].len(), 2);
        assert_eq!(paths[0][0].relation, "account");
        assert_eq!(paths[0][0].from_column, "account_id");
        assert_eq!(paths[0][1].to_table, "owner");
        let encoded = serde_json::to_value(&plan).expect("instances kind must serialize");
        assert_eq!(encoded, wire, "instances round-trip must not drift");
    }

    #[test]
    fn instances_fixture_roundtrip() {
        // The instances-plan golden vector (#285): an included query's full
        // payload — root predicate, empty `joins`, and a one-path instances
        // plan — survives a deserialize/serialize round-trip without drift.
        let fixture =
            include_str!("../../../tests/fixtures/ir_vectors/query_transaction_include_v7.json");
        let parsed: serde_json::Value =
            serde_json::from_str(fixture).expect("query include fixture must parse");
        let ir = parsed
            .get("ir")
            .cloned()
            .expect("fixture must contain ir envelope");
        let envelope: IrEnvelope<QueryIrPayload> =
            serde_json::from_value(ir.clone()).expect("query include IR must deserialize");
        let Materialization::Instances { paths } = &envelope.payload.materialization else {
            panic!(
                "expected an instances plan, got {:?}",
                envelope.payload.materialization
            );
        };
        // Include paths ride the materialization plan, never the `joins`
        // section (ADR-0008): the fixture pins an include-only query with an
        // EMPTY joins list and one single-hop path.
        assert!(envelope.payload.joins.is_empty());
        assert_eq!(paths.len(), 1);
        assert_eq!(paths[0].len(), 1);
        assert_eq!(paths[0][0].relation, "account");
        let encoded = serde_json::to_value(&envelope).expect("query include IR must serialize");
        assert_eq!(encoded, ir, "query include round-trip must not drift");
    }

    #[test]
    fn instances_without_paths_is_rejected() {
        // v4 `instances` always carries `paths`; a bare `{"kind": "instances"}`
        // (the v3 unit shape) must fail the strict parse loudly.
        let err = serde_json::from_value::<Materialization>(
            serde_json::json!({"kind": "instances"}),
        )
        .expect_err("instances without paths must fail to deserialize");
        assert!(
            err.to_string().contains("paths"),
            "must name the missing field: {err}"
        );
    }

    #[test]
    fn instances_with_malformed_paths_is_rejected() {
        // A hop missing its facts fails the strict parse loudly.
        let err = serde_json::from_value::<Materialization>(serde_json::json!({
            "kind": "instances",
            "paths": [[{"relation": "account"}]]
        }))
        .expect_err("a malformed hop must fail to deserialize");
        assert!(
            err.to_string().contains("from_column"),
            "must name the missing hop fact: {err}"
        );
    }

    #[test]
    fn unknown_materialization_kind_is_rejected() {
        let err =
            serde_json::from_value::<Materialization>(serde_json::json!({"kind": "row_dicts"}))
                .expect_err("an unknown materialization kind must fail to deserialize");
        let msg = err.to_string();
        assert!(msg.contains("row_dicts"), "must name the bad kind: {msg}");
        assert!(
            msg.contains("root_instances") && msg.contains("record") && msg.contains("instances"),
            "must name the supported kinds: {msg}"
        );
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
