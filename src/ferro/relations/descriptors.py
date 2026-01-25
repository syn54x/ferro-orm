from ..state import _MODEL_REGISTRY_PY


class RelationshipDescriptor:
    """Descriptor that returns either a Query object or a single object (for 1:1)."""

    def __init__(
        self,
        target_model_name: str,
        field_name: str,
        is_one_to_one: bool = False,
        is_m2m: bool = False,
        join_table: str | None = None,
        source_col: str | None = None,
        target_col: str | None = None,
    ):
        self.target_model_name = target_model_name
        self.field_name = field_name
        self.is_one_to_one = is_one_to_one
        self.is_m2m = is_m2m
        self.join_table = join_table
        self.source_col = source_col
        self.target_col = target_col
        self._target_model = None

    def __get__(self, instance, owner):
        if instance is None:
            return self

        if self._target_model is None:
            self._target_model = _MODEL_REGISTRY_PY.get(self.target_model_name)
            if self._target_model is None:
                raise RuntimeError(
                    f"Model '{self.target_model_name}' not found in registry"
                )

        if self.is_m2m:
            # Special Query for M2M that knows about the join table
            # For now we'll just return a regular query but we need to update
            # the Rust engine to support JOINs or subqueries.
            # We'll pass the M2M context to the Query object.
            from ..query.builder import Query

            return Query(self._target_model)._m2m(
                self.join_table, self.source_col, self.target_col, instance.id
            )

        query = self._target_model.where(
            getattr(self._target_model, f"{self.field_name}_id") == instance.id
        )

        if self.is_one_to_one:
            return query.first()

        return query


class ForwardDescriptor:
    """Descriptor that handles lazy loading of a related object."""

    def __init__(self, field_name: str, target_model_name: str):
        self.field_name = field_name
        self.target_model_name = target_model_name
        self._target_model = None

    def __get__(self, instance, owner):
        if instance is None:
            return self

        if self._target_model is None:
            self._target_model = _MODEL_REGISTRY_PY.get(self.target_model_name)
            if self._target_model is None:
                raise RuntimeError(
                    f"Model '{self.target_model_name}' not found in registry"
                )

        async def _fetch():
            id_val = getattr(instance, f"{self.field_name}_id")
            if id_val is None:
                return None
            return await self._target_model.get(id_val)

        return _fetch()
