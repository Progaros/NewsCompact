"""Syntax check of the vanilla-JS frontend (no Node needed: tree-sitter parses modern JS)."""
from pathlib import Path

import pytest

tree_sitter = pytest.importorskip("tree_sitter")
tree_sitter_javascript = pytest.importorskip("tree_sitter_javascript")

FRONTEND = Path(__file__).resolve().parents[1] / "app" / "frontend"


@pytest.mark.parametrize("path", sorted(FRONTEND.glob("*.js")), ids=lambda p: p.name)
def test_frontend_js_parses(path):
    parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_javascript.language()))
    root = parser.parse(path.read_bytes()).root_node
    bad, stack = [], [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            bad.append(node.start_point[0] + 1)
        stack.extend(node.children)
    assert not root.has_error, f"{path.name}: syntax error near line(s) {sorted(bad)[:5]}"
