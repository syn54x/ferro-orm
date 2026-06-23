from pathlib import Path


def test_query_methods_use_query_ir_serializer_instead_of_raw_json_dumps():
    source = Path("src/ferro/query/builder.py").read_text(encoding="utf-8")

    assert source.count("def _query_ir_payload_to_json(") == 1
    assert "json.dumps(query_def)" not in source
