"""FF-D D3 exit gate: exactly one route-resolution site per operation.

RouteHandle may only be constructed inside src/ferro/state.py (the two
resolvers). Rust must not re-derive routes per operation.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _grep(root: Path, pattern: str, suffix: str) -> list[str]:
    hits = []
    for path in root.rglob(f"*{suffix}"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern in line and not line.lstrip().startswith(("#", "//", "///")):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}")
    return hits


def test_route_handle_constructed_only_in_state_py():
    hits = _grep(ROOT / "src" / "ferro", "RouteHandle(", ".py")
    assert hits, "expected RouteHandle construction in src/ferro/state.py"
    assert all(h.startswith("src/ferro/state.py") for h in hits), hits


def test_rust_route_rederivation_is_gone():
    assert _grep(ROOT / "src", "active_route_for_operation", ".rs") == []
    assert _grep(ROOT / "src", "active_engine_for_connection", ".rs") == []
