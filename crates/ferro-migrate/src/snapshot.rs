//! The schema snapshot (`ir.json`): the declared SchemaIR modelset a
//! migration was generated from, stored as canonical JSON with the SHA-384 of
//! its parent's `ir.json` (ADR-0023).

use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};

/// One loaded `ir.json`.
#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    /// The declared modelset.
    pub ir: IrEnvelope<SchemaIrPayload>,
    /// SHA-384 of the file's bytes as stored.
    pub checksum: [u8; 48],
    /// SHA-384 of the parent migration's `ir.json`; `None` for `0001`.
    pub parent_checksum: Option<[u8; 48]>,
}

/// Why an `ir.json` could not be read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SnapshotError {
    /// What is wrong and what to do about it.
    pub message: String,
}

impl std::fmt::Display for SnapshotError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for SnapshotError {}

/// The schema `ir_version` this ferro writes.
pub const CURRENT_SCHEMA_IR_VERSION: u32 = 1;

/// The key beside the envelope's own that links a snapshot to its parent.
const PARENT_CHECKSUM_KEY: &str = "parent_checksum";

impl Snapshot {
    /// Load an `ir.json` written by any ferro version.
    ///
    /// Every schema `ir_version` ferro has shipped loads for good (ADR-0023):
    /// an older version goes through its own loader into today's IR types.
    /// An absent `parent_checksum` reads as no parent — the shape of a bare
    /// IR envelope, which the golden vectors pin; a migration's place in the
    /// chain is verified against its neighbour's checksum, never assumed.
    ///
    /// # Errors
    /// A [`SnapshotError`] naming the problem when the bytes are not JSON, not
    /// a `schema` IR, of an `ir_version` newer than this ferro reads, or carry a
    /// malformed `parent_checksum`.
    pub fn load(bytes: &[u8]) -> Result<Snapshot, SnapshotError> {
        let fail = |message: String| SnapshotError { message };
        let mut document: serde_json::Value = serde_json::from_slice(bytes)
            .map_err(|err| fail(format!("is not valid JSON ({err})")))?;
        let object = document
            .as_object_mut()
            .ok_or_else(|| fail("is not a JSON object".to_string()))?;
        let parent_checksum = match object.remove(PARENT_CHECKSUM_KEY) {
            None | Some(serde_json::Value::Null) => None,
            Some(serde_json::Value::String(hex)) => {
                Some(decode_checksum(&hex).ok_or_else(|| {
                    fail(format!(
                        "has parent_checksum {hex:?}, which is not a SHA-384 in hex (96 characters)"
                    ))
                })?)
            }
            Some(other) => {
                return Err(fail(format!(
                    "has parent_checksum {other}, which is neither null nor a hex SHA-384"
                )));
            }
        };
        let kind = object.get("ir_kind").and_then(|v| v.as_str()).unwrap_or("");
        if kind != "schema" {
            return Err(fail(format!(
                "has ir_kind '{kind}'; a schema snapshot is a 'schema' IR envelope"
            )));
        }
        let version = object
            .get("ir_version")
            .and_then(|v| v.as_u64())
            .ok_or_else(|| fail("has no numeric ir_version".to_string()))?;
        let ir = match version {
            1 => serde_json::from_value::<IrEnvelope<SchemaIrPayload>>(document)
                .map_err(|err| fail(format!("is not a valid schema IR v1 envelope ({err})")))?,
            other => {
                return Err(fail(format!(
                    "has ir_version {other}, which this ferro cannot read (it reads schema \
                     ir_version 1 through {CURRENT_SCHEMA_IR_VERSION}); it was written by a \
                     newer ferro, so upgrade ferro"
                )));
            }
        };
        Ok(Snapshot {
            ir,
            checksum: sha384(bytes),
            parent_checksum,
        })
    }

    /// The canonical bytes of the snapshot of `ir` whose parent is
    /// `parent_checksum`: the envelope plus `parent_checksum` (lowercase hex,
    /// or `null` for the first migration), every object's keys sorted,
    /// two-space indented, ending in a newline. The same modelset always
    /// stores to the same bytes.
    pub fn store(ir: &IrEnvelope<SchemaIrPayload>, parent_checksum: Option<[u8; 48]>) -> Vec<u8> {
        // The IR types are plain data (strings, numbers, vectors, options):
        // serializing them to a `Value` cannot fail.
        let mut document = serde_json::to_value(ir).unwrap_or(serde_json::Value::Null);
        if let Some(object) = document.as_object_mut() {
            object.insert(
                PARENT_CHECKSUM_KEY.to_string(),
                parent_checksum
                    .map(|checksum| serde_json::Value::String(encode_checksum(&checksum)))
                    .unwrap_or(serde_json::Value::Null),
            );
        }
        let mut bytes =
            serde_json::to_vec_pretty(&canonical(document)).unwrap_or_else(|_| b"null".to_vec());
        bytes.push(b'\n');
        bytes
    }
}

/// `value` with every object's keys in sorted order, whatever map
/// implementation `serde_json` was built with.
fn canonical(value: serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::Object(map) => {
            let sorted: std::collections::BTreeMap<String, serde_json::Value> =
                map.into_iter().map(|(k, v)| (k, canonical(v))).collect();
            serde_json::Value::Object(sorted.into_iter().collect())
        }
        serde_json::Value::Array(items) => {
            serde_json::Value::Array(items.into_iter().map(canonical).collect())
        }
        other => other,
    }
}

/// SHA-384 of `bytes`.
pub fn sha384(bytes: &[u8]) -> [u8; 48] {
    use sha2::Digest;
    sha2::Sha384::digest(bytes).into()
}

/// A checksum as lowercase hex, the spelling `ir.json` and every report use.
pub fn encode_checksum(checksum: &[u8; 48]) -> String {
    checksum.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// The checksum `hex` spells, when it spells one.
pub fn decode_checksum(hex: &str) -> Option<[u8; 48]> {
    if hex.len() != 96 || !hex.is_ascii() {
        return None;
    }
    let mut out = [0u8; 48];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&hex[i * 2..i * 2 + 2], 16).ok()?;
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn vectors_dir() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/ir_vectors")
    }

    /// Every golden schema vector, as the `ir` member of its vector file.
    fn schema_vectors() -> Vec<(String, Vec<u8>)> {
        let mut found = Vec::new();
        for entry in std::fs::read_dir(vectors_dir()).expect("ir_vectors directory") {
            let path = entry.expect("dir entry").path();
            let name = path.file_name().unwrap().to_string_lossy().to_string();
            if !name.starts_with("schema_") || !name.ends_with(".json") {
                continue;
            }
            let vector: serde_json::Value =
                serde_json::from_slice(&std::fs::read(&path).expect("read vector")).expect("json");
            let ir = serde_json::to_vec(&vector["ir"]).expect("serialize ir");
            found.push((name, ir));
        }
        found.sort();
        found
    }

    #[test]
    fn every_shipped_schema_vector_loads_through_the_snapshot_loader() {
        let vectors = schema_vectors();
        assert!(vectors.len() >= 3, "expected the golden schema vectors");
        for (name, bytes) in vectors {
            let snapshot = Snapshot::load(&bytes)
                .unwrap_or_else(|err| panic!("{name} must load: {}", err.message));
            assert_eq!(snapshot.ir.ir_kind, "schema", "{name}");
            assert!(!snapshot.ir.payload.models.is_empty(), "{name}");
            assert_eq!(snapshot.checksum, sha384(&bytes), "{name}");
            assert_eq!(snapshot.parent_checksum, None, "{name}");
        }
    }

    #[test]
    fn storing_the_same_modelset_twice_yields_identical_bytes_that_load_back() {
        for (name, bytes) in schema_vectors() {
            let loaded = Snapshot::load(&bytes).expect("load");
            let parent = Some(sha384(b"parent"));
            let first = Snapshot::store(&loaded.ir, parent);
            let second = Snapshot::store(&loaded.ir, parent);
            assert_eq!(first, second, "{name}: canonical bytes are deterministic");
            assert!(first.ends_with(b"\n"), "{name}: ends with a newline");
            let back = Snapshot::load(&first).expect("stored snapshot loads");
            assert_eq!(back.ir, loaded.ir, "{name}: the modelset round-trips");
            assert_eq!(back.parent_checksum, parent, "{name}");
            assert_eq!(back.checksum, sha384(&first), "{name}");
        }
    }

    #[test]
    fn stored_json_has_sorted_keys_and_a_hex_parent_checksum() {
        let (_, bytes) = schema_vectors().remove(0);
        let ir = Snapshot::load(&bytes).expect("load").ir;
        let root = Snapshot::store(&ir, None);
        let text = String::from_utf8(root).expect("utf-8");
        assert!(text.contains("\"parent_checksum\": null"), "{text}");
        let keys: Vec<&str> = text
            .lines()
            .filter(|line| line.starts_with("  \"") && !line.starts_with("    "))
            .map(|line| line.trim().split('"').nth(1).unwrap())
            .collect();
        assert_eq!(
            keys,
            ["ir_kind", "ir_version", "parent_checksum", "payload"]
        );

        let child = String::from_utf8(Snapshot::store(&ir, Some([0xab; 48]))).expect("utf-8");
        assert!(child.contains(&format!("\"parent_checksum\": \"{}\"", "ab".repeat(48))));
    }

    #[test]
    fn a_snapshot_from_a_newer_ferro_or_of_another_kind_is_refused() {
        let newer = br#"{"ir_kind": "schema", "ir_version": 999, "parent_checksum": null, "payload": {"dialect_agnostic": true, "models": []}}"#;
        let err = Snapshot::load(newer).expect_err("newer ir_version");
        assert!(err.message.contains("ir_version 999"), "{}", err.message);
        assert!(err.message.contains("upgrade ferro"), "{}", err.message);

        let query = br#"{"ir_kind": "query", "ir_version": 1, "payload": {}}"#;
        let err = Snapshot::load(query).expect_err("query kind");
        assert!(err.message.contains("'query'"), "{}", err.message);

        let err = Snapshot::load(b"{not json").expect_err("malformed");
        assert!(err.message.contains("not valid JSON"), "{}", err.message);

        let bad_parent = br#"{"ir_kind": "schema", "ir_version": 1, "parent_checksum": "abc", "payload": {"dialect_agnostic": true, "models": []}}"#;
        let err = Snapshot::load(bad_parent).expect_err("bad checksum");
        assert!(err.message.contains("parent_checksum"), "{}", err.message);
    }
}
