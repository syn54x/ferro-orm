from contextvars import ContextVar

# Context variable to store the active transaction ID for the current task
_CURRENT_TRANSACTION: ContextVar[str | None] = ContextVar(
    "current_transaction", default=None
)

# Global registry for models (Python side)
_MODEL_REGISTRY_PY = {}

# Global registry for relationships that need deferred resolution
_PENDING_RELATIONS = []
