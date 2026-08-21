"""Deterministic, Python-only ROS 2 node static extraction (Step 1).

The module deliberately exposes two representations: ``ModuleIR`` preserves
ordinary Python facts and ``ROSNodeIR`` interprets those facts as local ROS
entities.  It never imports or executes analyzed code.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import operator
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from ..schemas.ros import SCHEMA_VERSION, validate_step1_payload


IGNORED_DIRECTORIES = {
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".venv",
    "__pycache__", "build", "dist", "install", "log", "logs", "venv",
}
ENDPOINTS = {
    "create_publisher": ("publishers", ("msg_type", "topic", "qos_profile")),
    "create_subscription": (
        "subscriptions", ("msg_type", "topic", "callback", "qos_profile")
    ),
    "create_service": ("services", ("srv_type", "srv_name", "callback", "qos_profile")),
    "create_client": ("clients", ("srv_type", "srv_name", "qos_profile")),
    "create_timer": ("timers", ("timer_period_sec", "callback")),
}
TF_TYPES = {
    "tf2_ros.Buffer": "buffer", "tf2_ros.TransformListener": "listener",
    "tf2_ros.TransformBroadcaster": "broadcaster",
    "tf2_ros.StaticTransformBroadcaster": "static_broadcaster",
}
ACTIONS = {
    "rclpy.action.ActionServer": ("server", ("node", "action_type", "action_name", "execute_callback")),
    "rclpy.action.ActionClient": ("client", ("node", "action_type", "action_name")),
}


def _text(source: str, node: ast.AST | None) -> str | None:
    if node is None:
        return None
    return (ast.get_source_segment(source, node) or ast.unparse(node)).strip()


def _location(path: str, node: ast.AST) -> dict[str, Any]:
    return {
        "file": path, "line_start": getattr(node, "lineno", None),
        "column_start": getattr(node, "col_offset", None),
        "line_end": getattr(node, "end_lineno", None),
        "column_end": getattr(node, "end_col_offset", None),
    }


def _name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _name(node.func)
    if isinstance(node, ast.Subscript):
        return _name(node.value)
    return ""


def _target(node: ast.AST | None, source: str) -> str | None:
    return _text(source, node)


def _assigned_target(parent: ast.AST | None, call: ast.Call, source: str) -> str | None:
    if isinstance(parent, ast.Assign) and parent.value is call:
        return _target(parent.targets[0], source) if parent.targets else None
    if isinstance(parent, ast.AnnAssign) and parent.value is call:
        return _target(parent.target, source)
    return None


def _imports(tree: ast.Module) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for item in tree.body:
        if isinstance(item, ast.Import):
            for alias in item.names:
                local = alias.asname or alias.name.split(".")[0]
                result[local] = {"qualified_name": alias.name, "kind": "module"}
        elif isinstance(item, ast.ImportFrom):
            prefix = "." * item.level + (item.module or "")
            for alias in item.names:
                local = alias.asname or alias.name
                qualified = f"{prefix}.{alias.name}" if prefix else alias.name
                result[local] = {"qualified_name": qualified, "kind": "symbol"}
    return dict(sorted(result.items()))


def _qualify(name: str, imports: dict[str, dict[str, str]]) -> str:
    head, dot, tail = name.partition(".")
    imported = imports.get(head)
    if not imported:
        return name
    return imported["qualified_name"] + (dot + tail if dot else "")


class Resolver:
    """Small safe evaluator which retains symbolic dependencies."""

    BINOPS = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow,
    }

    def __init__(self, source: str, values: dict[str, Any] | None = None) -> None:
        self.source = source
        self.values = values or {}

    def value(self, node: ast.AST | None) -> tuple[bool, Any]:
        if node is None:
            return True, None
        expression = _text(self.source, node)
        if expression in self.values:
            return True, self.values[expression]
        if isinstance(node, ast.Constant):
            return True, node.value
        if isinstance(node, ast.Name) and node.id in self.values:
            return True, self.values[node.id]
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            parts = [self.value(item) for item in node.elts]
            if all(ok for ok, _ in parts):
                values = [value for _, value in parts]
                return True, tuple(values) if isinstance(node, ast.Tuple) else values
        if isinstance(node, ast.Dict):
            keys = [self.value(item) for item in node.keys]
            vals = [self.value(item) for item in node.values]
            if all(ok for ok, _ in keys + vals):
                return True, {key: value for (_, key), (_, value) in zip(keys, vals)}
        if isinstance(node, ast.UnaryOp):
            ok, value = self.value(node.operand)
            if ok:
                try:
                    if isinstance(node.op, ast.USub): return True, -value
                    if isinstance(node.op, ast.UAdd): return True, +value
                    if isinstance(node.op, ast.Not): return True, not value
                except (TypeError, ValueError):
                    pass
        if isinstance(node, ast.BinOp) and type(node.op) in self.BINOPS:
            left_ok, left = self.value(node.left)
            right_ok, right = self.value(node.right)
            if left_ok and right_ok:
                try:
                    return True, self.BINOPS[type(node.op)](left, right)
                except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                    pass
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(str(value.value))
                elif isinstance(value, ast.FormattedValue):
                    ok, resolved = self.value(value.value)
                    if not ok: break
                    parts.append(str(resolved))
            else:
                return True, "".join(parts)
        return False, None

    def expression(self, node: ast.AST | None) -> dict[str, Any]:
        raw = _text(self.source, node)
        ok, value = self.value(node)
        dependencies = [] if ok else _dependencies(node, self.source)
        return {
            "expression": raw,
            "resolved_value": value if ok else None,
            "resolved": ok,
            "dependencies": dependencies,
            "resolution": {
                "status": "resolved" if ok else ("partial" if dependencies else "unresolved"),
                "method": "static_evaluation" if ok else "symbolic",
                "reason": None if ok else ("dynamic_expression" if dependencies else "unknown_expression"),
            },
        }


def _dependencies(node: ast.AST | None, source: str) -> list[str]:
    if node is None:
        return []
    values: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Load):
            values.add(_text(source, item) or _name(item))
        elif isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load):
            values.add(item.id)
    # Retain the longest attribute, not each of its component Name nodes.
    return sorted(value for value in values if value not in {"self", "True", "False", "None"})


def _parameters(node: ast.arguments) -> list[str]:
    return [item.arg for item in (*node.posonlyargs, *node.args, *node.kwonlyargs)]


def _condition_context(tree: ast.AST, source: str) -> dict[int, tuple[list[str], list[dict[str, str]]]]:
    result: dict[int, tuple[list[str], list[dict[str, str]]]] = {}

    def walk(node: ast.AST, conditions: list[str], loops: list[dict[str, str]]) -> None:
        result[id(node)] = (conditions, loops)
        if isinstance(node, ast.If):
            test = _text(source, node.test) or "<condition>"
            walk(node.test, conditions, loops)
            for child in node.body: walk(child, conditions + [test], loops)
            for child in node.orelse: walk(child, conditions + [f"not ({test})"], loops)
            return
        if isinstance(node, (ast.For, ast.AsyncFor)):
            context = {"variable": _text(source, node.target) or "<target>",
                       "iterable": _text(source, node.iter) or "<iterable>"}
            walk(node.target, conditions, loops)
            walk(node.iter, conditions, loops)
            for child in node.body: walk(child, conditions, loops + [context])
            for child in node.orelse: walk(child, conditions, loops)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, conditions, loops)

    walk(tree, [], [])
    return result


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    return {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _assignment_record(item: ast.Assign | ast.AnnAssign, source: str, path: str,
                       resolver: Resolver) -> list[dict[str, Any]]:
    targets = item.targets if isinstance(item, ast.Assign) else [item.target]
    return [{"target": _target(target, source), "value": resolver.expression(item.value),
             "source": _location(path, item)} for target in targets]


def _function_record(node: ast.FunctionDef | ast.AsyncFunctionDef, source: str,
                     path: str) -> dict[str, Any]:
    assignments, calls, returns = [], [], []
    for item in ast.walk(node):
        if item is not node and isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(item, (ast.Assign, ast.AnnAssign)):
            assignments.extend(_assignment_record(item, source, path, Resolver(source)))
        elif isinstance(item, ast.Call):
            calls.append({"expression": _text(source, item), "callee": _name(item.func),
                          "arguments": [_text(source, arg) for arg in item.args],
                          "keywords": {kw.arg: _text(source, kw.value) for kw in item.keywords if kw.arg},
                          "source": _location(path, item)})
        elif isinstance(item, ast.Return):
            returns.append({"value": Resolver(source).expression(item.value),
                            "source": _location(path, item)})
    key = lambda record: (record["source"]["line_start"] or 0, record["source"]["column_start"] or 0)
    return {"name": node.name, "parameters": _parameters(node.args),
            "assignments": sorted(assignments, key=key), "calls": sorted(calls, key=key),
            "returns": sorted(returns, key=key), "source": _location(path, node)}


def build_module_ir(source: str, *, file_path: str, module_name: str,
                    package: str | None = None) -> tuple[dict[str, Any], ast.Module]:
    """Parse one Python source string into a reusable ModuleIR."""
    tree = ast.parse(source, filename=file_path)
    imports = _imports(tree)
    resolver = Resolver(source)
    globals_: list[dict[str, Any]] = []
    classes, functions = [], []
    for item in tree.body:
        if isinstance(item, (ast.Assign, ast.AnnAssign)):
            records = _assignment_record(item, source, file_path, resolver)
            globals_.extend(records)
            for record in records:
                if record["target"] and record["value"]["resolved"]:
                    resolver.values[record["target"]] = record["value"]["resolved_value"]
        elif isinstance(item, ast.ClassDef):
            class_attrs, methods = [], []
            for child in item.body:
                if isinstance(child, (ast.Assign, ast.AnnAssign)):
                    class_attrs.extend(_assignment_record(child, source, file_path, resolver))
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(_function_record(child, source, file_path))
            classes.append({"name": item.name,
                            "bases": [{"expression": _text(source, base),
                                       "resolved": _qualify(_name(base), imports)} for base in item.bases],
                            "class_attributes": class_attrs, "methods": methods,
                            "source": _location(file_path, item)})
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_function_record(item, source, file_path))
    known = set(imports) | {r["target"] for r in globals_ if r["target"]}
    known |= {item["name"] for item in classes + functions}
    unresolved: dict[str, list[dict[str, Any]]] = {}
    for item in ast.walk(tree):
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load) and item.id not in known:
            if item.id in dir(__builtins__) or item.id in {"self", "cls"}: continue
            unresolved.setdefault(item.id, []).append(_location(file_path, item))
    entrypoints = []
    for function in (item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))):
        instantiated = []
        for call in (child for child in ast.walk(function) if isinstance(child, ast.Call)):
            if _name(call.func) in {item["name"] for item in classes}:
                instantiated.append({"class": _name(call.func), "source": _location(file_path, call)})
        if instantiated:
            entrypoints.append({"function": function.name, "node_instantiations": instantiated})
    ros_factories = []
    for function_node in (item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))):
        creates = []
        for call in (item for item in ast.walk(function_node) if isinstance(item, ast.Call)):
            suffix = _name(call.func).split(".")[-1]
            if suffix in ENDPOINTS:
                creates.append({"kind": ENDPOINTS[suffix][0][:-1], "expression": _text(source, call),
                                "source": _location(file_path, call)})
            qualified = _qualify(_name(call.func), imports)
            if qualified in ACTIONS:
                creates.append({"kind": f"action_{ACTIONS[qualified][0]}", "expression": _text(source, call),
                                "source": _location(file_path, call)})
        if creates:
            ros_factories.append({"function": function_node.name, "ros_factory": True, "creates": creates,
                                  "source": _location(file_path, function_node)})
    module_payload = {"schema_version": SCHEMA_VERSION,
             "module": {"file_path": file_path, "module_name": module_name, "package": package},
             "imports": imports, "global_assignments": globals_, "classes": classes,
             "functions": functions, "entrypoints": entrypoints, "ros_factories": ros_factories,
             "unresolved_symbols": [{"symbol": key, "locations": value}
                                    for key, value in sorted(unresolved.items())]}
    return (module_payload, tree)


def _call_args(call: ast.Call, names: tuple[str, ...]) -> dict[str, ast.AST]:
    result = dict(zip(names, call.args))
    result.update({kw.arg: kw.value for kw in call.keywords if kw.arg})
    return result


def _id(kind: str, path: str, class_name: str, node: ast.AST) -> str:
    raw = f"{path}|{class_name}|{kind}|{getattr(node, 'lineno', 0)}|{getattr(node, 'col_offset', 0)}"
    return f"{kind}:{hashlib.sha1(raw.encode()).hexdigest()[:12]}"


def _get_parameter(call_or_chain: ast.AST, resolver: Resolver) -> str | None:
    for child in ast.walk(call_or_chain):
        if isinstance(child, ast.Call) and _name(child.func).split(".")[-1] == "get_parameter":
            if child.args:
                ok, value = resolver.value(child.args[0])
                return value if ok and isinstance(value, str) else None
    return None


def _condition_values(conditions: list[str], resolver: Resolver) -> list[dict[str, Any]]:
    values = []
    for condition in conditions:
        try:
            node = ast.parse(condition, mode="eval").body
        except SyntaxError:
            values.append({"expression": condition, "resolved_value": None, "resolved": False,
                           "dependencies": [], "resolution": {"status": "unresolved",
                           "method": "symbolic", "reason": "unparseable_condition"}})
        else:
            condition_resolver = Resolver(condition, resolver.values)
            values.append(condition_resolver.expression(node))
    return values


def _node_ir(module_ir: dict[str, Any], tree: ast.Module, class_node: ast.ClassDef,
             source: str) -> dict[str, Any]:
    path = module_ir["module"]["file_path"]
    imports = module_ir["imports"]
    values = {item["target"]: item["value"]["resolved_value"]
              for item in module_ir["global_assignments"] if item["value"]["resolved"]}
    resolver = Resolver(source, values)
    parents = _parent_map(class_node)
    contexts = _condition_context(class_node, source)
    assignments: dict[str, ast.AST] = {}
    parameter_bindings: dict[str, str] = {}
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": f"node:{module_ir['module']['module_name']}.{class_node.name}",
        "class_name": class_node.name, "qualified_class": f"{module_ir['module']['module_name']}.{class_node.name}",
        "ros_node_status": "confirmed",
        "node_name": resolver.expression(None),
        "source": _location(path, class_node), "parameters": [], "qos_profiles": [],
        "publishers": [], "subscriptions": [], "services": [], "clients": [],
        "timers": [], "tf_entities": [], "actions": [], "functions": [],
        "relationships": [], "unresolved_expressions": [],
    }
    # First collect assignments and parameter reads so endpoint resolution is order-independent.
    for item in ast.walk(class_node):
        if isinstance(item, (ast.Assign, ast.AnnAssign)):
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            for target in targets:
                target_text = _text(source, target)
                if target_text:
                    assignments[target_text] = item.value
                    parameter = _get_parameter(item.value, resolver)
                    if parameter:
                        parameter_bindings[target_text] = parameter
                        result["relationships"].append({"from": f"parameter:{parameter}",
                                                        "to": target_text, "kind": "assigned_to",
                                                        "source": _location(path, item)})
    # Propagate simple defaults through assignments for deterministic local resolution.
    parameter_defaults: dict[str, Any] = {}
    calls = sorted((item for item in ast.walk(class_node) if isinstance(item, ast.Call)),
                   key=lambda item: (item.lineno, item.col_offset))
    for call in calls:
        suffix = _name(call.func).split(".")[-1]
        if suffix == "declare_parameter":
            args = _call_args(call, ("name", "value", "descriptor"))
            name_expr = resolver.expression(args.get("name"))
            default = resolver.expression(args.get("value"))
            name = name_expr["resolved_value"] if name_expr["resolved"] else None
            if isinstance(name, str) and default["resolved"]:
                parameter_defaults[name] = default["resolved_value"]
            result["parameters"].append({"id": _id("parameter", path, class_node.name, call),
                "name": name_expr, "default": default,
                "descriptor": resolver.expression(args.get("descriptor")), "reads": [],
                "conditions": _condition_values(contexts[id(call)][0], resolver),
                "source": _location(path, call)})
        elif suffix == "declare_parameters":
            args = _call_args(call, ("namespace", "parameters"))
            params = args.get("parameters")
            namespace = resolver.value(args.get("namespace"))[1] or ""
            if isinstance(params, (ast.List, ast.Tuple)):
                for entry in params.elts:
                    if not isinstance(entry, (ast.Tuple, ast.List)) or not entry.elts: continue
                    name_expr = resolver.expression(entry.elts[0])
                    default = resolver.expression(entry.elts[1] if len(entry.elts) > 1 else None)
                    name = name_expr["resolved_value"] if name_expr["resolved"] else None
                    if isinstance(name, str):
                        name_expr["resolved_value"] = f"{namespace}.{name}".strip(".")
                        parameter_defaults[name_expr["resolved_value"]] = default["resolved_value"]
                    result["parameters"].append({"id": _id("parameter", path, class_node.name, entry),
                        "name": name_expr, "default": default,
                        "descriptor": resolver.expression(entry.elts[2] if len(entry.elts) > 2 else None),
                        "reads": [], "conditions": _condition_values(contexts[id(call)][0], resolver),
                        "source": _location(path, entry)})
    for target, parameter in parameter_bindings.items():
        if parameter in parameter_defaults:
            values[target] = parameter_defaults[parameter]
    # Resolve ordinary assignments to a fixed point (e.g. period = 1 / parameter attribute).
    for _ in range(len(assignments) + 1):
        changed = False
        resolver.values = values
        for target, value_node in assignments.items():
            ok, value = resolver.value(value_node)
            if ok and values.get(target) != value:
                values[target] = value; changed = True
        if not changed: break
    resolver.values = values
    by_parameter = {p["name"]["resolved_value"]: p for p in result["parameters"] if p["name"]["resolved"]}
    for target, parameter in parameter_bindings.items():
        if parameter in by_parameter:
            by_parameter[parameter]["reads"].append({"assigned_to": target,
                "source": next((r["source"] for r in result["relationships"] if r["to"] == target), None)})
    # Interpret calls only after symbols/defaults are available.
    for call in calls:
        full_name = _name(call.func)
        suffix = full_name.split(".")[-1]
        conditions, loops = contexts.get(id(call), ([], []))
        parent = parents.get(id(call))
        variable = _assigned_target(parent, call, source)
        owner = call
        while id(owner) in parents and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents[id(owner)]
        created_in = owner.name if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<class>"
        if suffix == "__init__" and full_name.startswith("super") and call.args:
            result["node_name"] = resolver.expression(call.args[0])
        if suffix in ENDPOINTS:
            collection, arg_names = ENDPOINTS[suffix]
            args = _call_args(call, arg_names)
            expansions: list[tuple[str | None, Any]] = [(None, None)]
            if loops:
                loop = loops[-1]
                iterable = values.get(loop["iterable"])
                if iterable is None:
                    try: iterable = ast.literal_eval(loop["iterable"])
                    except (ValueError, SyntaxError): pass
                if isinstance(iterable, (list, tuple)):
                    expansions = [(loop["variable"], value) for value in iterable]
            for expansion_index, (loop_variable, loop_value) in enumerate(expansions):
                local_values = dict(values)
                if loop_variable is not None: local_values[loop_variable] = loop_value
                local_resolver = Resolver(source, local_values)
                entity_id = _id(collection[:-1], path, class_node.name, call)
                if len(expansions) > 1: entity_id += f":{expansion_index}"
                record: dict[str, Any] = {"id": entity_id, "variable": variable,
                    "created_in": created_in, "origin_class": class_node.name, "inherited": False,
                    "conditions": _condition_values(conditions, resolver),
                    "loop_context": loops, "generated_from_loop": loop_variable is not None,
                    "dynamic_multiplicity": bool(loops) and loop_variable is None,
                    "source": _location(path, call)}
                if collection in {"publishers", "subscriptions"}:
                    type_node = args.get("msg_type")
                    record["message_type"] = {"expression": _text(source, type_node),
                                              "resolved": _qualify(_name(type_node), imports)}
                    record["topic"] = local_resolver.expression(args.get("topic"))
                    record["qos"] = local_resolver.expression(args.get("qos_profile"))
                elif collection in {"services", "clients"}:
                    type_node = args.get("srv_type")
                    record["service_type"] = {"expression": _text(source, type_node),
                                              "resolved": _qualify(_name(type_node), imports)}
                    record["name"] = local_resolver.expression(args.get("srv_name"))
                    record["qos"] = local_resolver.expression(args.get("qos_profile"))
                else:
                    record["period"] = local_resolver.expression(args.get("timer_period_sec"))
                if "callback" in args: record["callback"] = _text(source, args["callback"])
                result[collection].append(record)
                expression_key = "topic" if "topic" in record else "name" if "name" in record else "period"
                for dependency in record[expression_key]["dependencies"]:
                    origin = f"parameter:{parameter_bindings.get(dependency)}" if dependency in parameter_bindings else dependency
                    result["relationships"].append({"from": origin, "to": record["id"],
                                                    "kind": f"controls_{expression_key}",
                                                    "source": _location(path, call)})
                if record.get("callback"):
                    result["relationships"].append({"from": record["id"], "to": record["callback"],
                                                    "kind": "invokes_callback", "source": _location(path, call)})
            continue
        qualified = _qualify(full_name, imports)
        if qualified in ACTIONS:
            action_kind, arg_names = ACTIONS[qualified]
            args = _call_args(call, arg_names)
            callbacks = {name: _text(source, value) for name, value in args.items() if name.endswith("callback")}
            callbacks.update({kw.arg: _text(source, kw.value) for kw in call.keywords
                              if kw.arg and kw.arg.endswith("callback")})
            type_node = args.get("action_type")
            result["actions"].append({"id": _id("action", path, class_node.name, call),
                "kind": action_kind, "variable": variable, "created_in": created_in,
                "origin_class": class_node.name, "inherited": False,
                "action_type": {"expression": _text(source, type_node),
                                "resolved": _qualify(_name(type_node), imports)},
                "name": resolver.expression(args.get("action_name")), "callbacks": callbacks,
                "conditions": _condition_values(conditions, resolver), "source": _location(path, call)})
            continue
        if qualified.endswith("QoSProfile"):
            fields = {kw.arg: resolver.expression(kw.value) for kw in call.keywords if kw.arg}
            result["qos_profiles"].append({"id": _id("qos", path, class_node.name, call),
                "variable": variable, "created_in": created_in, "origin_class": class_node.name,
                "inherited": False, "fields": fields,
                "conditions": _condition_values(conditions, resolver),
                "source": _location(path, call)})
        if qualified in TF_TYPES:
            result["tf_entities"].append({"id": _id("tf", path, class_node.name, call),
                "kind": TF_TYPES[qualified], "variable": variable, "created_in": created_in,
                "origin_class": class_node.name, "inherited": False, "constructor": qualified,
                "conditions": _condition_values(conditions, resolver),
                "source": _location(path, call)})
    # Behavior facts and lightweight callback/method relationships.
    endpoint_variables = {item["variable"]: item["id"] for item in result["publishers"] if item["variable"]}
    for method in (item for item in class_node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))):
        behavior = {"name": method.name, "calls": [], "publish_calls": [],
                    "service_calls": [], "tf_calls": [], "source": _location(path, method)}
        for call in sorted((x for x in ast.walk(method) if isinstance(x, ast.Call)), key=lambda x: (x.lineno, x.col_offset)):
            name = _name(call.func)
            fact = {"callee": name, "expression": _text(source, call), "source": _location(path, call)}
            behavior["calls"].append(fact)
            if name.endswith(".publish"):
                behavior["publish_calls"].append(fact)
                owner = name[:-8]
                if owner in endpoint_variables:
                    result["relationships"].append({"from": method.name, "to": endpoint_variables[owner],
                                                    "kind": "publishes_via", "source": fact["source"]})
            elif name.endswith((".call", ".call_async")):
                behavior["service_calls"].append(fact)
            elif name.endswith((".sendTransform", ".lookup_transform", ".can_transform")):
                behavior["tf_calls"].append(fact)
        result["functions"].append(behavior)
    # Inventory all unresolved entity expressions, retaining provenance.
    for collection in ("parameters", "publishers", "subscriptions", "services", "clients", "timers"):
        for record in result[collection]:
            for field in ("name", "default", "topic", "period", "qos"):
                expression = record.get(field)
                if isinstance(expression, dict) and not expression.get("resolved") and expression.get("expression") is not None:
                    result["unresolved_expressions"].append({"owner_id": record["id"], "field": field,
                        **expression, "source": record["source"]})
    result["relationships"].sort(key=lambda x: (x["source"]["line_start"] or 0, x["kind"], x["to"]))
    return result


def extract_ros_node_ir(workspace: str | Path, *, source_roots: Iterable[str | Path] = ("src",)) -> dict[str, Any]:
    """Extract ModuleIR and ROSNodeIR records from Python files under roots."""
    workspace = Path(workspace).resolve()
    roots = [(workspace / root).resolve() if not Path(root).is_absolute() else Path(root).resolve()
             for root in source_roots]
    paths = sorted({path for root in roots if root.is_dir() for path in root.rglob("*.py")
                    if not any(part in IGNORED_DIRECTORIES for part in path.parts) and ".launch." not in path.name})
    modules, nodes, failures = [], [], []
    for path in paths:
        relative = path.relative_to(workspace).as_posix()
        root = next((root for root in roots if path.is_relative_to(root)), workspace)
        module_name = ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(".__init__")
        package = path.relative_to(root).parts[0] if path.relative_to(root).parts else None
        try:
            source = path.read_text(encoding="utf-8")
            module, tree = build_module_ir(source, file_path=relative, module_name=module_name, package=package)
        except (OSError, UnicodeError, SyntaxError) as error:
            failures.append({"file": relative, "error": str(error)}); continue
        modules.append(module)
        class_nodes = {item.name: item for item in tree.body if isinstance(item, ast.ClassDef)}
        class_irs = {item["name"]: item for item in module["classes"]}
        ros_status: dict[str, bool] = {}

        def is_ros_class(name: str, visiting: set[str] | None = None) -> bool:
            if name in ros_status: return ros_status[name]
            visiting = set() if visiting is None else visiting
            if name in visiting: return False
            visiting.add(name)
            record = class_irs[name]
            direct = any(base["resolved"] in {"rclpy.node.Node", "rclpy.lifecycle.LifecycleNode"}
                         for base in record["bases"])
            local_bases = [_name(base) for base in class_nodes[name].bases if _name(base) in class_irs]
            result = direct or any(is_ros_class(base, visiting) for base in local_bases)
            ros_status[name] = result
            return result

        local_nodes: dict[str, dict[str, Any]] = {}
        for class_ir in module["classes"]:
            if is_ros_class(class_ir["name"]):
                local_nodes[class_ir["name"]] = _node_ir(
                    module, tree, class_nodes[class_ir["name"]], source
                )

        merge_collections = ("parameters", "qos_profiles", "publishers", "subscriptions",
                             "services", "clients", "actions", "timers", "tf_entities")
        merged: set[str] = set()

        def merge_inheritance(name: str) -> dict[str, Any]:
            node = local_nodes[name]
            if name in merged: return node
            local_bases = [_name(base) for base in class_nodes[name].bases if _name(base) in local_nodes]
            for base_name in local_bases:
                base = merge_inheritance(base_name)
                id_map: dict[str, str] = {}
                for collection in merge_collections:
                    inherited_records = []
                    for original in base[collection]:
                        record = copy.deepcopy(original)
                        old_id = record["id"]
                        record["id"] = f"{old_id}:inherited:{name}"
                        id_map[old_id] = record["id"]
                        record["inherited"] = True
                        record["inherited_from"] = base_name
                        inherited_records.append(record)
                    node[collection] = inherited_records + node[collection]
                for relationship in base["relationships"]:
                    inherited = copy.deepcopy(relationship)
                    inherited["from"] = id_map.get(inherited["from"], inherited["from"])
                    inherited["to"] = id_map.get(inherited["to"], inherited["to"])
                    inherited["inherited"] = True
                    node["relationships"].append(inherited)
                if not node["node_name"]["resolved"] and base["node_name"]["resolved"]:
                    node["node_name"] = copy.deepcopy(base["node_name"])
                    node["node_name"]["resolution"]["method"] = "local_inheritance"
            merged.add(name)
            return node

        nodes.extend(merge_inheritance(name) for name in local_nodes)
    counts = Counter()
    for node in nodes:
        for key in ("parameters", "qos_profiles", "publishers", "subscriptions", "services",
                    "clients", "actions", "timers", "tf_entities", "unresolved_expressions"):
            counts[key] += len(node[key])
    payload = {"schema_version": SCHEMA_VERSION, "stage": "python_ros_static_extraction",
            "boundary": {"python_only": True, "launch": False, "yaml": False,
                         "inter_node_edges": False, "llm": False},
            "summary": {"modules": len(modules), "nodes": len(nodes),
                        **dict(sorted(counts.items())), "parse_failures": len(failures)},
            "parse_failures": failures, "modules": modules, "nodes": nodes}
    return validate_step1_payload(payload).model_dump(mode="json")


def write_ros_node_ir(payload: dict[str, Any], output_directory: str | Path) -> Path:
    """Write Step 1 output in deterministic JSON form."""
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    validated = validate_step1_payload(payload)
    path = output / "ros_node_ir.json"
    path.write_text(validated.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
