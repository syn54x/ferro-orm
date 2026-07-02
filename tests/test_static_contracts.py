import ast
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
