"""Static SDK names must be visible without importing the optional Feishu SDK."""

import ast
from pathlib import Path


def test_lazy_feishu_sdk_names_are_declared_without_eager_imports():
    adapter = Path(__file__).resolve().parents[2] / "plugins/platforms/feishu/adapter.py"
    module = ast.parse(adapter.read_text(encoding="utf-8"))
    names = set()
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "_LARK_SDK_IMPORTS" for target in node.targets):
            for _, sdk_names in ast.literal_eval(node.value):
                names.update(sdk_names)
    assert names, "lazy SDK import table was not found"
    declared = set()
    for node in module.body:
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"):
            continue
        for statement in node.body:
            if (isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Name)
                    and statement.value.id == "Any"):
                declared.update(target.id for target in statement.targets if isinstance(target, ast.Name))
    assert names <= declared, f"lazy SDK names missing static declarations: {sorted(names - declared)}"
