from pathlib import Path


def test_query_methods_use_query_def_serializer_instead_of_raw_json_dumps():
    source = Path("src/ferro/query/builder.py").read_text(encoding="utf-8")

    assert source.count("json.dumps(_serialize_query_value(query_def))") == 1
    assert "json.dumps(query_def)" not in source
