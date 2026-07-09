"""FF-E E3 exit gate: defining N models costs O(N) schema builds, not O(N²).

Instruments build_column_specs (the expensive Pydantic-schema pass, now living
in ferro.columns) and counts calls across a 200-model synthetic fixture. The
old per-class compile_registry_schema_ir() recompiled every registered model
on every class definition — ~N²/2 builds for N models.
"""


from ferro import Model


def test_import_cost_is_linear_in_model_count(monkeypatch):
    import ferro.ir.compiler as ir_compiler

    calls = {"n": 0}
    real = ir_compiler.build_column_specs

    def counting(model_cls):
        calls["n"] += 1
        return real(model_cls)

    monkeypatch.setattr(ir_compiler, "build_column_specs", counting)

    n_models = 200
    for i in range(n_models):
        type(
            f"BudgetModel{i}",
            (Model,),
            {"__annotations__": {"id": int | None, "name": str}, "id": None},
        )

    # One build per class definition (the metaclass builds; the per-model IR
    # compile reuses it). 2×N headroom tolerates one extra build per class,
    # but O(N²) (~20,000 for N=200) must fail loudly.
    assert calls["n"] <= 2 * n_models, calls["n"]
