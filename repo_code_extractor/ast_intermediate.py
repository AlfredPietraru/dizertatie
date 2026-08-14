"""Lossless JSON conversion for Python AST nodes used between pipeline stages."""

from __future__ import annotations

import ast
from typing import Any


LOCATION_ATTRIBUTES = ("lineno", "col_offset", "end_lineno", "end_col_offset")


def ast_to_json(node: Any) -> Any:
    if isinstance(node, ast.AST):
        result = {"_type": type(node).__name__}
        for field in node._fields:
            result[field] = ast_to_json(getattr(node, field, None))
        for attribute in LOCATION_ATTRIBUTES:
            if hasattr(node, attribute):
                result[attribute] = getattr(node, attribute)
        return result
    if isinstance(node, list):
        return [ast_to_json(item) for item in node]
    if node is Ellipsis:
        return {"_literal": "ellipsis"}
    if isinstance(node, bytes):
        return {"_literal": "bytes", "hex": node.hex()}
    if isinstance(node, complex):
        return {"_literal": "complex", "real": node.real, "imag": node.imag}
    return node


def ast_from_json(value: Any) -> Any:
    if isinstance(value, list):
        return [ast_from_json(item) for item in value]
    if isinstance(value, dict) and value.get("_literal") == "ellipsis":
        return Ellipsis
    if isinstance(value, dict) and value.get("_literal") == "bytes":
        return bytes.fromhex(value["hex"])
    if isinstance(value, dict) and value.get("_literal") == "complex":
        return complex(value["real"], value["imag"])
    if not isinstance(value, dict) or "_type" not in value:
        return value
    node_type = getattr(ast, value["_type"])
    fields = {
        key: ast_from_json(item)
        for key, item in value.items()
        if key != "_type" and key not in LOCATION_ATTRIBUTES
    }
    node = node_type(**fields)
    for attribute in LOCATION_ATTRIBUTES:
        if attribute in value:
            setattr(node, attribute, value[attribute])
    return node
