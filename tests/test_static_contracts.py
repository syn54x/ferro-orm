import ast
import subprocess
from pathlib import Path


def test_query_methods_use_query_ir_serializer_instead_of_raw_json_dumps():
    source = Path("src/ferro/query/builder.py").read_text(encoding="utf-8")

    assert source.count("def _query_ir_payload_to_json(") == 1
    assert "json.dumps(query_def)" not in source


def test_mutating_query_methods_cannot_carry_pagination():
    source = Path("src/ferro/query/builder.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    query_cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Query"
    )
    for method_name in ("update", "delete"):
        method = next(
            node
            for node in query_cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
        )
        attributes = {
            node.attr for node in ast.walk(method) if isinstance(node, ast.Attribute)
        }
        assert "_limit" not in attributes, (
            f"Query.{method_name}() must not read _limit; pagination is rejected "
            "by _mutating_query_def (FF-A A1)"
        )
        assert "_offset" not in attributes, (
            f"Query.{method_name}() must not read _offset; pagination is rejected "
            "by _mutating_query_def (FF-A A1)"
        )
        assert "_mutating_query_def" in attributes, (
            f"Query.{method_name}() must build its payload via _mutating_query_def"
        )


def _models_module_tree() -> ast.Module:
    source = Path("src/ferro/models.py").read_text(encoding="utf-8")
    return ast.parse(source)


def _class_def(tree: ast.Module, class_name: str) -> ast.ClassDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )


def _method_def(cls: ast.ClassDef, method_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    )


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def test_create_paths_never_pass_on_conflict():
    """create() stays a plain INSERT: no create-family path may reach the
    upsert surface (FF-A A3)."""
    tree = _models_module_tree()
    model_cls = _class_def(tree, "Model")
    connection_cls = _class_def(tree, "ModelConnection")
    methods = [
        _method_def(model_cls, "create"),
        _method_def(model_cls, "get_or_create"),
        _method_def(model_cls, "update_or_create"),
        _method_def(connection_cls, "create"),
    ]
    for method in methods:
        for node in ast.walk(method):
            if not isinstance(node, ast.Call):
                continue
            if _called_name(node) not in ("save", "create"):
                continue
            keywords = {kw.arg for kw in node.keywords}
            assert "on_conflict" not in keywords, (
                f"{method.name}() must not pass on_conflict; create paths are "
                "plain INSERTs (FF-A A3)"
            )


def test_save_dispatches_by_persistence_state():
    """save() routes transient instances to INSERT, persisted ones to UPDATE,
    and only the explicit on_conflict surface to upsert (FF-A A4)."""
    tree = _models_module_tree()
    save = _method_def(_class_def(tree, "Model"), "save")
    called = {
        _called_name(node) for node in ast.walk(save) if isinstance(node, ast.Call)
    }
    assert "update_record" in called, "save() must UPDATE persisted instances"
    assert "save_record" in called
    assert "_is_persisted" in called, "save() must dispatch on persistence state"

    for node in ast.walk(save):
        if not (isinstance(node, ast.Call) and _called_name(node) == "save_record"):
            continue
        mode_kw = next((kw for kw in node.keywords if kw.arg == "mode"), None)
        assert mode_kw is not None, "save_record calls must pass an explicit mode"
        assert isinstance(mode_kw.value, ast.Constant)
        assert mode_kw.value.value in ("insert", "upsert")


def test_bulk_create_payload_has_no_conflict_handling():
    """bulk_create stays a plain multi-row INSERT."""
    tree = _models_module_tree()
    bulk_create = _method_def(_class_def(tree, "Model"), "bulk_create")
    for node in ast.walk(bulk_create):
        if not (
            isinstance(node, ast.Call) and _called_name(node) == "save_bulk_records"
        ):
            continue
        keywords = {kw.arg for kw in node.keywords}
        assert (
            "mode" not in keywords and "on_conflict" not in keywords
        ), "bulk_create must not grow conflict handling implicitly"


FIXTURES = Path(__file__).parent / "static_fixtures"


def _run_ty(target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "ty", "check", "--output-format", "concise", str(target)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )


def test_valid_lambda_predicates_pass_the_type_checker():
    result = _run_ty(FIXTURES / "good_predicates.py")
    assert result.returncode == 0, (
        f"good predicates must type-check cleanly:\n{result.stdout}\n{result.stderr}"
    )


def test_junk_lambda_predicates_fail_the_type_checker():
    """`lambda t: True` (bool, not QueryNode) must be rejected statically.

    Bare-lambda RHS typing (`u.age >= "x"`) is NOT checkable today: it
    requires TypeScript-style mapped types (PEP 827, draft, targeting
    Python 3.16). When checkers support it, QueryProxy's attribute typing
    upgrades from FieldProxy[Any] with zero runtime change.
    """
    result = _run_ty(FIXTURES / "bad_predicates.py")
    assert result.returncode != 0, "bad_predicates.py must fail ty"
    assert "invalid-assignment" in result.stdout


def test_projected_shape_misuse_fails_the_type_checker():
    """A `Row` where a model instance is expected must be rejected statically
    (#279), and mutating a projected query is a static error at the call site
    (#280, the `self: Never` pin)."""
    result = _run_ty(FIXTURES / "bad_projections.py")
    assert result.returncode != 0, "bad_projections.py must fail ty"
    assert "invalid-assignment" in result.stdout
    assert result.stdout.count("invalid-argument-type") >= 2, (
        "update() and delete() on a projected query must each be a static error"
    )


def test_query_ir_is_the_only_query_shape_in_rust():
    """FF-F F-4 exit gate: no `struct QueryNode` outside the IR crate."""
    repo = Path(__file__).parent.parent
    rust_roots = [repo / "src", repo / "crates"]
    ir_crate = repo / "crates" / "ferro-schema-ir"
    offenders = []
    for root in rust_roots:
        for rust_file in root.rglob("*.rs"):
            if ir_crate in rust_file.parents:
                continue
            if "struct QueryNode" in rust_file.read_text(encoding="utf-8"):
                offenders.append(str(rust_file))
    assert offenders == [], (
        f"legacy QueryNode shape resurfaced outside crates/ferro-schema-ir: {offenders}"
    )


def test_operator_predicate_surface_is_gone():
    nodes_src = Path("src/ferro/query/nodes.py").read_text(encoding="utf-8")
    assert "def col(" not in nodes_src
    assert "predicate_style" not in nodes_src
    builder_src = Path("src/ferro/query/builder.py").read_text(encoding="utf-8")
    assert "_deprecated_operator_query_node" not in builder_src
    metaclass_src = Path("src/ferro/metaclass.py").read_text(encoding="utf-8")
    assert "FieldProxy(" not in metaclass_src
