#!/usr/bin/env python3
"""Static, dependency-free checks for a QMT/MiniQMT strategy.

The checker is intentionally conservative: it validates syntax and visible
order guards, but it cannot prove that a broker, account, or QMT backtest is
available.  Use the JSON output as an audit artifact, not as a trading signal.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path


def read_source(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gbk", "cp936"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", raw, 0, min(len(raw), 1), "unsupported source encoding")


def assignment_values(tree: ast.AST) -> dict[str, object]:
    values: dict[str, object] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        try:
            constant = ast.literal_eval(value)
        except Exception:
            constant = "<non-literal>"
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id] = constant
    return values


def call_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def function_names(tree: ast.AST) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def validate(path: Path) -> dict[str, object]:
    result: dict[str, object] = {
        "path": str(path.resolve()),
        "status": "pass",
        "encoding": None,
        "errors": [],
        "warnings": [],
        "checks": {},
    }
    errors = result["errors"]
    warnings = result["warnings"]
    checks = result["checks"]

    if not path.exists() or not path.is_file():
        errors.append("strategy file does not exist")
        result["status"] = "fail"
        return result

    try:
        source, encoding = read_source(path)
        result["encoding"] = encoding
        tree = ast.parse(source, filename=str(path))
        compile(source, str(path), "exec")
        checks["syntax"] = "pass"
    except (SyntaxError, UnicodeError) as exc:
        errors.append("syntax/encoding: %s" % exc)
        checks["syntax"] = "fail"
        result["status"] = "fail"
        return result

    values = assignment_values(tree)
    funcs = function_names(tree)
    calls = call_names(tree)

    checks["init_handlebar"] = "pass" if {"init", "handlebar"}.issubset(funcs) else "fail"
    if checks["init_handlebar"] == "fail":
        errors.append("QMT entry points init(C) and handlebar(C) are both required")

    checks["orders_disabled"] = "pass" if values.get("ENABLE_ORDERS") is False else "fail"
    if checks["orders_disabled"] == "fail":
        errors.append("ENABLE_ORDERS must be a literal False by default")

    checks["backtest_gate"] = "pass" if "BACKTEST_ORDERS" in values else "fail"
    if checks["backtest_gate"] == "fail":
        errors.append("BACKTEST_ORDERS gate is missing")

    token_names = {"LIVE_TOKEN", "LIVE_CONFIRM_TOKEN", "LIVE_TOKEN_REQUIRED"}
    checks["live_token"] = "pass" if token_names.intersection(values) else "fail"
    if checks["live_token"] == "fail":
        errors.append("an explicit live confirmation token is required")

    checks["universe"] = "pass" if "set_universe" in calls else "warn"
    if checks["universe"] == "warn":
        warnings.append("C.set_universe was not found; verify the strategy sets its universe")

    history_calls = {"get_history_data", "get_market_data_ex", "get_market_data"}
    checks["history_api"] = "pass" if history_calls.intersection(calls) else "warn"
    if checks["history_api"] == "warn":
        warnings.append("no recognized QMT history-data call; verify the data adapter")

    has_order_call = bool({"passorder", "order", "order_target_value", "order_target"}.intersection(calls))
    checks["order_gate_path"] = "pass" if (not has_order_call or {"can_route_orders", "_send_order"}.intersection(funcs)) else "warn"
    if checks["order_gate_path"] == "warn":
        warnings.append("order-like call found without an obvious can_route_orders/_send_order wrapper")

    if has_order_call:
        quick_trade_hint = bool(re.search(r"quickTrade", source, re.IGNORECASE)) or bool(
            re.search(r"passorder\s*\([\s\S]{0,1200}?\b0\s*,", source)
        )
        checks["quick_trade_zero"] = "pass" if quick_trade_hint else "warn"
        if not quick_trade_hint:
            warnings.append("quickTrade=0 was not detected; verify the order argument explicitly")
    else:
        checks["quick_trade_zero"] = "not_applicable"

    if errors:
        result["status"] = "fail"
    elif warnings:
        result["status"] = "pass_with_warnings"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("strategy", type=Path)
    parser.add_argument("--json-out", type=Path, help="write the result JSON to this path")
    args = parser.parse_args()
    result = validate(args.strategy)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n", encoding="utf-8")
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
