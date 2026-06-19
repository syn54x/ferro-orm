from contextvars import ContextVar
from typing import Any

# Context variable to store the active transaction ID for the current task
_CURRENT_TRANSACTION: ContextVar[str | None] = ContextVar(
    "current_transaction", default=None
)

_CURRENT_TRANSACTION_CONNECTION: ContextVar[str | None] = ContextVar(
    "current_transaction_connection", default=None
)

# Global registry for models (Python side)
_MODEL_REGISTRY_PY = {}

# Global registry for relationships that need deferred resolution
_PENDING_RELATIONS = []

# Global registry for automatically generated join tables
_JOIN_TABLE_REGISTRY = {}

# Latest compiled SchemaIR model-set artifact and fingerprint.
_SCHEMA_IR_MODELSET: dict[str, Any] | None = None
_SCHEMA_IR_MODELSET_FINGERPRINT: str | None = None

# Per-model compiled SchemaIR artifacts and fingerprints.
_SCHEMA_IR_BY_MODEL: dict[str, dict[str, Any]] = {}
_SCHEMA_IR_FINGERPRINT_BY_MODEL: dict[str, str] = {}
