# Exceptions

Exceptions raised by Ferro's public API. `ModelDoesNotExist` is raised by primary-key lookups like `Model.get(pk)` when no row matches; use `Model.get_or_none(pk)` if you prefer `None` over an exception.

::: ferro.ModelDoesNotExist
