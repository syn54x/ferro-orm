"""Public SchemaIR compilation API for the Python runtime."""

from .compiler import (
    compile_model_schema_ir,
    compile_registry_schema_ir,
    register_model_with_ir,
    schema_ir_fingerprint,
)

__all__ = [
    "compile_model_schema_ir",
    "compile_registry_schema_ir",
    "register_model_with_ir",
    "schema_ir_fingerprint",
]
