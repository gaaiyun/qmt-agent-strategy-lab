#!/usr/bin/env python3
"""Build a read-only QMT native-validation manifest from files and logs.

The tool never imports QMT/XtQuant modules and never calls a trading API.  It
only reads strategy sources, deployment copies, validation JSON, screenshots,
and ``XtClient_Formula_*.log`` files.  Any observed order account other than
``BACKTEST`` fails the manifest safety gate.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")
REQID_RE = re.compile(
    r"(?P<reqid>0D:\\.*?\\(?P<model>QMT_[A-Z0-9_]+)\.py_(?P<suffix>[A-Za-z0-9]+))",
    re.IGNORECASE,
)
FIELD_PATTERNS = {
    "account_id": re.compile(r"\baccountID:([^,\s]+)"),
    "quick_trade": re.compile(r"\bquickTrade:([-+]?\d+)"),
    "operation": re.compile(r"\bopType:([-+]?\d+)"),
    "bar_position": re.compile(r"\bbarpos:([-+]?\d+)"),
    "universe_count": re.compile(r"\bsl:\[(\d+),"),
}
SAFETY_NAMES = {
    "ENABLE_ORDERS",
    "BACKTEST_ORDERS",
    "LIVE_TOKEN",
    "LIVE_CONFIRM_TOKEN",
    "LIVE_TOKEN_REQUIRED",
    "VALID_ACCOUNTS",
}
UNIVERSE_NAMES = {
    "UNIVERSE", "RISK_UNIVERSE", "DEFENSIVE_SYMBOL", "TARGET_SYMBOL", "SYMBOL"
}
WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/])")
WINDOWS_USERNAME_RE = re.compile(r"[A-Za-z]:[\\/]Users[\\/]([^\\/]+)", re.IGNORECASE)
SENSITIVE_NAME_MARKERS = (
    "PASSWORD", "PASSWD", "APIKEY", "TOKEN", "SECRET", "CREDENTIAL",
    "VALIDACCOUNTS", "USERNAME",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def read_source(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gbk", "cp936"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeError("unsupported source encoding: %s" % path)


def _safe_eval(node: ast.AST, values: dict[str, Any]) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        pass
    if isinstance(node, ast.Name) and node.id in values:
        return values[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _safe_eval(node.left, values)
        right = _safe_eval(node.right, values)
        if isinstance(left, (str, tuple, list)) and isinstance(right, type(left)):
            return left + right
    return None


def _jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _is_sensitive_name(name: str) -> bool:
    compact = re.sub(r"[^A-Za-z0-9]", "", name).upper()
    return any(marker in compact for marker in SENSITIVE_NAME_MARKERS)


def analyze_source(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": path.is_file(),
        "path": str(path.resolve()),
        "sha256": None,
        "modified_at": None,
        "encoding": None,
        "syntax": "not_checked",
        "parameters": {},
        "universe_symbols": [],
        "universe_count": None,
        "safety": {},
    }
    if not path.is_file():
        result["syntax"] = "missing"
        return result

    source, encoding = read_source(path)
    result.update(
        sha256=sha256_file(path),
        modified_at=datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(),
        encoding=encoding,
    )
    try:
        tree = ast.parse(source, filename=str(path))
        compile(source, str(path), "exec")
        result["syntax"] = "pass"
    except SyntaxError as exc:
        result["syntax"] = "fail"
        result["syntax_error"] = str(exc)
        return result

    values: dict[str, Any] = {}
    attribute_symbols: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        target_names = [target.id for target in targets if isinstance(target, ast.Name)]
        if any(_is_sensitive_name(name) and name not in SAFETY_NAMES for name in target_names):
            continue
        value = _safe_eval(node.value, values)
        for target in targets:
            if isinstance(target, ast.Name) and value is not None:
                values[target.id] = value
            elif (
                isinstance(target, ast.Attribute)
                and target.attr == "symbol"
                and isinstance(value, str)
            ):
                attribute_symbols.append(value)

    universe: list[str] = []
    candidate = values.get("UNIVERSE")
    if isinstance(candidate, (list, tuple)):
        universe = [str(item) for item in candidate]
    elif isinstance(values.get("RISK_UNIVERSE"), (list, tuple)):
        universe = [str(item) for item in values["RISK_UNIVERSE"]]
        defensive = values.get("DEFENSIVE_SYMBOL")
        if isinstance(defensive, str):
            universe.append(defensive)
    elif isinstance(values.get("TARGET_SYMBOL"), str):
        universe = [values["TARGET_SYMBOL"]]
    elif isinstance(values.get("SYMBOL"), str):
        universe = [values["SYMBOL"]]
    elif attribute_symbols:
        universe = attribute_symbols
    result["universe_symbols"] = list(dict.fromkeys(universe))
    result["universe_count"] = len(result["universe_symbols"]) or None

    for name, value in sorted(values.items()):
        if not name.isupper() or name in SAFETY_NAMES or name in UNIVERSE_NAMES:
            continue
        if _jsonable(value):
            result["parameters"][name] = value

    token = values.get("LIVE_CONFIRM_TOKEN", values.get("LIVE_TOKEN"))
    accounts = values.get("VALID_ACCOUNTS")
    result["safety"] = {
        "enable_orders": values.get("ENABLE_ORDERS"),
        "backtest_orders": values.get("BACKTEST_ORDERS"),
        "live_confirmation_is_empty": token == "" if token is not None else None,
        "account_allowlist_is_empty": (
            accounts in (None, (), []) if accounts is not None else None
        ),
        "source_gate_safe": (
            values.get("ENABLE_ORDERS") is False
            and (token in (None, ""))
            and (accounts in (None, (), []))
        ),
    }
    return result


def inspect_file(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": path.is_file(),
        "path": str(path.resolve()),
        "sha256": None,
        "bytes": None,
        "modified_at": None,
        "storage_format": None,
    }
    if not path.is_file():
        return result
    raw_prefix = path.read_bytes()[:96]
    if raw_prefix.startswith((b"# coding", b"# -*- coding")):
        storage = "plain_source"
    elif raw_prefix.startswith(b"MiFBO") or (raw_prefix and b"\x00" not in raw_prefix):
        storage = "qmt_encoded_payload"
    else:
        storage = "unknown"
    result.update(
        sha256=sha256_file(path),
        bytes=path.stat().st_size,
        modified_at=datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(),
        storage_format=storage,
    )
    return result


def _timestamp(line: str) -> str | None:
    match = TIMESTAMP_RE.match(line)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f").astimezone().isoformat()


def _new_run(reqid: str, model: str) -> dict[str, Any]:
    return {
        "reqid": reqid,
        "qmt_model_name": model.upper(),
        "start_at": None,
        "complete_at": None,
        "start_count": 0,
        "calc_backtest_index_count": 0,
        "history_callback_count": 0,
        "backtest_passorder_calls_observed": 0,
        "non_backtest_account_order_actions_observed": False,
        "passorder_observation_semantics": "function_call_only_not_acceptance_or_fill",
        "buy_calls": 0,
        "sell_calls": 0,
        "accounts_observed": [],
        "quick_trade_values_observed": [],
        "bar_positions": [],
        "universe_count_observed": None,
        "log_files": [],
    }


def parse_qmt_logs(log_paths: Iterable[Path], model_names: set[str]) -> dict[str, Any]:
    models = {name.upper(): {"parse_complete_count": 0, "save_complete_count": 0,
                             "py_module_success_count": 0, "last_parser_event_at": None}
              for name in model_names}
    runs: dict[str, dict[str, Any]] = {}
    for log_path in sorted(log_paths):
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                upper = line.upper()
                if "QMT_" not in upper:
                    continue
                timestamp = _timestamp(line)
                for model_name in model_names:
                    if model_name not in upper:
                        continue
                    stats = models[model_name]
                    if "PARSE COMPLETE, NAME = %s" % model_name in upper:
                        stats["parse_complete_count"] += 1
                        stats["last_parser_event_at"] = timestamp
                    if "SAVE COMPLETE, NAME = %s" % model_name in upper:
                        stats["save_complete_count"] += 1
                        stats["last_parser_event_at"] = timestamp
                    if "PYMODULE_NEW %s SUCCEED" % model_name in upper:
                        stats["py_module_success_count"] += 1

                req_match = REQID_RE.search(line)
                if not req_match:
                    continue
                model = req_match.group("model").upper()
                if model not in model_names:
                    continue
                reqid = req_match.group("reqid")
                run = runs.setdefault(reqid, _new_run(reqid, model))
                if str(log_path.resolve()) not in run["log_files"]:
                    run["log_files"].append(str(log_path.resolve()))
                if "start back test mode" in line:
                    run["start_count"] += 1
                    run["start_at"] = run["start_at"] or timestamp
                if "calc backtest index" in line:
                    run["calc_backtest_index_count"] += 1
                    run["complete_at"] = timestamp
                if "historyCallback" in line:
                    run["history_callback_count"] += 1
                universe_match = FIELD_PATTERNS["universe_count"].search(line)
                if universe_match:
                    run["universe_count_observed"] = int(universe_match.group(1))
                if "[PYTHON PASSORDER]" not in line:
                    continue
                account_match = FIELD_PATTERNS["account_id"].search(line)
                account = account_match.group(1) if account_match else None
                if account == "BACKTEST":
                    run["backtest_passorder_calls_observed"] += 1
                else:
                    run["non_backtest_account_order_actions_observed"] = True
                operation = FIELD_PATTERNS["operation"].search(line)
                if operation and operation.group(1) == "23":
                    run["buy_calls"] += 1
                elif operation and operation.group(1) == "24":
                    run["sell_calls"] += 1
                for field in ("account_id", "quick_trade", "bar_position"):
                    match = FIELD_PATTERNS[field].search(line)
                    if not match:
                        continue
                    value: Any = match.group(1)
                    if field != "account_id":
                        value = int(value)
                    target = {
                        "account_id": "accounts_observed",
                        "quick_trade": "quick_trade_values_observed",
                        "bar_position": "bar_positions",
                    }[field]
                    run[target].append(value)

    by_model: dict[str, list[dict[str, Any]]] = {name: [] for name in model_names}
    for run in runs.values():
        run["accounts_observed"] = sorted(set(run["accounts_observed"]))
        run["quick_trade_values_observed"] = sorted(set(run["quick_trade_values_observed"]))
        positions = run.pop("bar_positions")
        run["minimum_bar_position"] = min(positions) if positions else None
        run["maximum_bar_position"] = max(positions) if positions else None
        by_model[run["qmt_model_name"]].append(run)
    for model_name, entries in by_model.items():
        entries.sort(key=lambda row: (row["start_at"] or "", row["reqid"]))
        models[model_name]["run_count"] = len(entries)
        models[model_name]["all_accounts_observed"] = sorted({
            account for entry in entries for account in entry["accounts_observed"]
        })
        models[model_name]["all_quick_trade_values_observed"] = sorted({
            value for entry in entries for value in entry["quick_trade_values_observed"]
        })
        models[model_name]["total_backtest_passorder_calls_observed"] = sum(
            entry["backtest_passorder_calls_observed"] for entry in entries
        )
        models[model_name]["non_backtest_account_order_actions_observed"] = any(
            entry["non_backtest_account_order_actions_observed"] for entry in entries
        )
        models[model_name]["passorder_observation_semantics"] = (
            "function_call_only_not_acceptance_or_fill"
        )
        models[model_name]["latest_run"] = entries[-1] if entries else None
    return models


def _normalize_path(value: str) -> str:
    return str(Path(value)).replace("/", "\\").lower()


def load_validations(validation_root: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for path in sorted(validation_root.glob("validation*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        output.append({"artifact_path": str(path.resolve()), "payload": payload})
    return output


def validations_for_model(
    validations: list[dict[str, Any]], model_name: str, source_path: Path, deployment_path: Path
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    source_name = source_path.name.lower()
    for item in validations:
        payload = item["payload"]
        strategy = payload.get("strategy", {}) if isinstance(payload, dict) else {}
        candidates = [
            payload.get("path") if isinstance(payload, dict) else None,
            strategy.get("project_source") if isinstance(strategy, dict) else None,
        ]
        recorded_model = strategy.get("qmt_model_name") if isinstance(strategy, dict) else None
        if not (
            (isinstance(recorded_model, str) and recorded_model.upper() == model_name.upper())
            or any(isinstance(value, str) and Path(value).name.lower() == source_name for value in candidates)
        ):
            continue
        recorded_source = next((value for value in candidates if isinstance(value, str)), None)
        source_hash = strategy.get("project_source_sha256") if isinstance(strategy, dict) else None
        deployment_hash = strategy.get("deployed_copy_sha256") if isinstance(strategy, dict) else None
        matches.append(
            {
                "artifact_path": item["artifact_path"],
                "schema": payload.get("schema") if isinstance(payload, dict) else None,
                "status": payload.get("status", payload.get("research_decision", {}).get("status"))
                if isinstance(payload, dict) else None,
                "recorded_source_path": recorded_source,
                "recorded_source_exists": Path(recorded_source).is_file() if recorded_source else None,
                "recorded_source_path_matches_current": (
                    _normalize_path(recorded_source) == _normalize_path(str(source_path.resolve()))
                    if recorded_source else None
                ),
                "recorded_source_sha256": source_hash,
                "source_sha256_matches_current": (
                    source_hash.upper() == sha256_file(source_path) if source_hash and source_path.is_file() else None
                ),
                "recorded_deployment_sha256": deployment_hash,
                "deployment_sha256_matches_current": (
                    deployment_hash.upper() == sha256_file(deployment_path)
                    if deployment_hash and deployment_path.is_file() else None
                ),
            }
        )
    return matches


def inspect_screenshots(entries: list[dict[str, Any]], source_root: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for entry in entries:
        path = Path(entry["path"])
        if not path.is_absolute():
            path = source_root / path
        output.append(
            {
                "kind": entry.get("kind", "unspecified"),
                "path": str(path.resolve()),
                "exists": path.is_file(),
                "sha256": sha256_file(path) if path.is_file() else None,
            }
        )
    return output


def _matching_hash_validation(validations: list[dict[str, Any]]) -> bool:
    return any(
        item.get("source_sha256_matches_current") is True
        and item.get("deployment_sha256_matches_current") is True
        for item in validations
    )


def build_manifest(
    config: dict[str, Any], source_root: Path, deployment_root: Path,
    log_paths: list[Path], validation_root: Path
) -> dict[str, Any]:
    configured_models = config.get("models", [])
    model_names = {item["qmt_model_name"].upper() for item in configured_models}
    logs = parse_qmt_logs(log_paths, model_names)
    validations = load_validations(validation_root)
    allowed_accounts = set(config.get("allowed_order_accounts", ["BACKTEST"]))
    models: list[dict[str, Any]] = []

    for configured in configured_models:
        model_name = configured["qmt_model_name"].upper()
        source_path = source_root / configured["source_file"]
        deployment_path = deployment_root / configured.get("deployment_file", model_name + ".py")
        source = analyze_source(source_path)
        deployment = inspect_file(deployment_path)
        model_validations = validations_for_model(
            validations, model_name, source_path, deployment_path
        )
        screenshots = inspect_screenshots(configured.get("screenshots", []), source_root)
        performance_panel = configured.get(
            "performance_panel", {"status": "not_captured", "metrics": {}}
        )
        log_evidence = logs[model_name]
        latest = log_evidence.get("latest_run")
        observed_accounts = set(log_evidence.get("all_accounts_observed", []))
        observed_quick_trade = set(log_evidence.get("all_quick_trade_values_observed", []))
        source_count = source.get("universe_count")
        observed_count = latest.get("universe_count_observed") if latest else None
        universe_matches = (
            source_count == observed_count if source_count is not None and observed_count is not None else None
        )
        source_after_deployment = False
        if source_path.is_file() and deployment_path.is_file():
            source_after_deployment = source_path.stat().st_mtime > deployment_path.stat().st_mtime
        source_after_run = False
        if source.get("modified_at") and latest and latest.get("complete_at"):
            source_after_run = source["modified_at"] > latest["complete_at"]

        safety_status = "pass"
        safety_reasons: list[str] = []
        if not source.get("safety", {}).get("source_gate_safe"):
            safety_status = "fail"
            safety_reasons.append("source live-order defaults are not fail-closed")
        unexpected_accounts = sorted(observed_accounts - allowed_accounts)
        if unexpected_accounts:
            safety_status = "fail"
            safety_reasons.append("one or more non-BACKTEST order accounts were observed")
        if observed_quick_trade - {0}:
            safety_status = "fail"
            safety_reasons.append("quickTrade value other than 0 observed")

        validation_hash_closed = _matching_hash_validation(model_validations)
        raw_hash_closed = bool(
            source.get("sha256")
            and deployment.get("sha256")
            and source["sha256"] == deployment["sha256"]
        )
        plain_hash_mismatch = bool(
            deployment.get("storage_format") == "plain_source"
            and source.get("sha256")
            and deployment.get("sha256")
            and source["sha256"] != deployment["sha256"]
        )
        hash_closed = validation_hash_closed or raw_hash_closed
        native_run_complete = bool(
            latest
            and latest.get("start_count", 0)
            and latest.get("calc_backtest_index_count", 0)
        )
        performance_screenshot_exists = any(
            screenshot.get("exists")
            and any(
                marker in screenshot.get("kind", "").lower()
                for marker in ("performance", "result", "backtest_panel")
            )
            for screenshot in screenshots
        )
        performance_panel_confirmed = bool(
            isinstance(performance_panel, dict)
            and performance_panel.get("status") in {"confirmed_by_screenshot", "verified"}
            and performance_screenshot_exists
        )
        if (
            hash_closed
            and universe_matches is True
            and not source_after_deployment
            and not source_after_run
            and native_run_complete
            and performance_panel_confirmed
        ):
            closure_status = "verified"
            closure_reason = (
                "current hashes, native run, universe, and performance screenshot form a complete closure"
                if validation_hash_closed
                else "matching plain source/deployment hashes, native run, universe, and performance screenshot form a complete closure"
            )
        elif source_after_deployment or source_after_run or universe_matches is False or plain_hash_mismatch:
            closure_status = "blocked"
            reasons = []
            if source_after_deployment:
                reasons.append("source is newer than the deployment copy")
            if source_after_run:
                reasons.append("source is newer than the latest native run")
            if universe_matches is False:
                reasons.append("source/native universe counts differ")
            if plain_hash_mismatch:
                reasons.append("plain deployment hash differs from the current source")
            closure_reason = "; ".join(reasons)
        else:
            closure_status = "partial"
            reasons = []
            if not hash_closed:
                reasons.append("no artifact binds both current source and deployment hashes")
            if universe_matches is None:
                reasons.append("native run universe count is not confirmed")
            if not native_run_complete:
                reasons.append("native run completion is not confirmed")
            if not performance_panel_confirmed:
                reasons.append("native performance panel screenshot is not confirmed")
            closure_reason = "; ".join(reasons) or "complete closure evidence is incomplete"

        compile_screenshot = any(
            "compile" in screenshot.get("kind", "") and screenshot.get("exists")
            for screenshot in screenshots
        )
        validation_compile_visible = any(
            isinstance(item.get("payload"), dict)
            and item["payload"].get("native_execution", {}).get("compile_success_visible") is True
            for item in validations
            if (
                item["payload"].get("strategy", {}).get("qmt_model_name", "").upper()
                == model_name
            )
        )
        models.append(
            {
                "qmt_model_name": model_name,
                "strategy_name": configured.get("strategy_name"),
                "strategy_type": configured.get("strategy_type"),
                "source": source,
                "deployment": deployment,
                "native_backtest_parameters": configured.get("native_backtest_parameters"),
                "log_evidence": log_evidence,
                "compile_evidence": {
                    "qmt_parser_completed": log_evidence.get("parse_complete_count", 0) > 0,
                    "py_module_success_logged": log_evidence.get("py_module_success_count", 0) > 0,
                    "compile_success_visible": compile_screenshot or validation_compile_visible,
                    "native_code_executed": bool(
                        native_run_complete
                    ),
                },
                "screenshots": screenshots,
                "performance_panel": performance_panel,
                "validations": model_validations,
                "safety": {
                    "status": safety_status,
                    "reasons": safety_reasons,
                    "allowed_order_accounts": sorted(allowed_accounts),
                    "unexpected_order_accounts": unexpected_accounts,
                    "non_backtest_account_order_actions_observed": bool(unexpected_accounts),
                },
                "version_closure": {
                    "status": closure_status,
                    "reason": closure_reason,
                    "hash_bound_by_validation": validation_hash_closed,
                    "plain_source_deployment_hash_equal": raw_hash_closed,
                    "source_universe_count": source_count,
                    "native_run_universe_count": observed_count,
                    "universe_count_matches": universe_matches,
                    "source_newer_than_deployment": source_after_deployment,
                    "source_newer_than_latest_native_run": source_after_run,
                    "native_run_complete": native_run_complete,
                    "performance_panel_confirmed": performance_panel_confirmed,
                },
            }
        )

    deployed_names = {
        path.name.upper() for path in deployment_root.glob("QMT_*.py") if path.is_file()
    }
    configured_names = {
        configured.get("deployment_file", configured["qmt_model_name"] + ".py").upper()
        for configured in configured_models
    }
    unsafe_models = [item["qmt_model_name"] for item in models if item["safety"]["status"] != "pass"]
    verified_closures = sum(
        item["version_closure"]["status"] == "verified" for item in models
    )
    partial_closures = sum(
        item["version_closure"]["status"] == "partial" for item in models
    )
    blocked_closures = sum(
        item["version_closure"]["status"] == "blocked" for item in models
    )
    if models and verified_closures == len(models):
        native_validation_status = "verified"
    elif not models or blocked_closures:
        native_validation_status = "blocked"
    else:
        native_validation_status = "partial"

    return {
        "schema": "qmt_native_handoff_manifest/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "generator": {
            "mode": "read_only",
            "imports_qmt_or_xtquant": False,
            "calls_trading_api": False,
            "allowed_order_accounts": sorted(allowed_accounts),
        },
        "roots": {
            "source": str(source_root.resolve()),
            "deployment": str(deployment_root.resolve()),
            "validation": str(validation_root.resolve()),
            "logs": [str(path.resolve()) for path in log_paths],
        },
        "summary": {
            "configured_models": len(models),
            "deployed_qmt_models": len(deployed_names),
            "unconfigured_deployments": sorted(deployed_names - configured_names),
            "missing_deployments": sorted(configured_names - deployed_names),
            "unsafe_models": unsafe_models,
            "safety_status": "pass" if not unsafe_models else "fail",
            "native_validation_status": native_validation_status,
            "verified_version_closures": verified_closures,
            "partial_version_closures": partial_closures,
            "blocked_version_closures": blocked_closures,
        },
        "models": models,
    }


def _evidence_id(value: str) -> str:
    digest = hashlib.sha256(value.replace("/", "\\").lower().encode("utf-8")).hexdigest()
    return "evidence/%s" % digest[:16].upper()


def _reqid_id(value: str) -> str:
    if value.startswith("reqid/"):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return "reqid/%s" % digest[:16].upper()


def _mask_account(value: str) -> str:
    if value == "BACKTEST" or value.startswith("ACCOUNT_SHA256_"):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return "ACCOUNT_SHA256_%s" % digest[:12].upper()


def _private_literals(value: Any, key: str = "") -> set[str]:
    literals: set[str] = set()
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            literals.update(_private_literals(child_value, str(child_key)))
    elif isinstance(value, list):
        for child in value:
            literals.update(_private_literals(child, key))
    elif isinstance(value, str):
        for match in WINDOWS_USERNAME_RE.finditer(value):
            literals.add(match.group(1))
        if "account" in key.lower() and value != "BACKTEST":
            literals.add(value)
        if _is_sensitive_name(key):
            literals.add(value)
    return {literal for literal in literals if literal}


def _sanitize_value(value: Any, key: str, private_literals: set[str]) -> Any:
    key_lower = key.lower()
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for child_key, child_value in value.items():
            child_key_str = str(child_key)
            if _is_sensitive_name(child_key_str):
                continue
            sanitized[child_key_str] = _sanitize_value(
                child_value, child_key_str, private_literals
            )
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(child, key, private_literals) for child in value]
    if not isinstance(value, str):
        return value
    if key_lower == "reqid":
        return _reqid_id(value)
    if "account" in key_lower:
        return _mask_account(value)
    if (
        key_lower in {"path", "artifact_path", "recorded_source_path", "log_files"}
        or key_lower.endswith("_path")
        or WINDOWS_ABSOLUTE_PATH_RE.search(value)
        or value.startswith("/")
    ):
        if value.startswith("evidence/"):
            return value
        return _evidence_id(value)
    sanitized = value
    for literal in sorted(private_literals, key=len, reverse=True):
        if literal in sanitized:
            replacement = _mask_account(literal) if literal.isdigit() else "[REDACTED]"
            sanitized = sanitized.replace(literal, replacement)
    return sanitized


def sanitize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a public projection with stable evidence and account identifiers."""
    sanitized = _sanitize_value(manifest, "", _private_literals(manifest))
    sanitized["schema"] = "qmt_native_handoff_manifest/sanitized-v2"
    sanitized["visibility"] = "sanitized"
    sanitized.setdefault("generator", {})["output_contains_private_evidence"] = False
    return sanitized


def render_markdown(manifest: dict[str, Any]) -> str:
    manifest = sanitize_manifest(manifest)
    summary = manifest["summary"]
    lines = [
        "# QMT 原生验收 manifest",
        "",
        "生成时间：`%s`。本文件由只读工具生成；未连接账户、未调用交易接口。" % manifest["created_at"],
        "",
        "## 总结",
        "",
        "- 配置模型：`%s`；部署目录 QMT 模型：`%s`。" % (
            summary["configured_models"], summary["deployed_qmt_models"]
        ),
        "- 安全状态：`%s`；发现非 BACKTEST 订单账户的模型：`%s`。" % (
            summary["safety_status"], ", ".join(summary["unsafe_models"]) or "无"
        ),
        "- 版本闭环：verified=%s，partial=%s，blocked=%s；公开状态：`%s`。" % (
            summary["verified_version_closures"],
            summary["partial_version_closures"],
            summary["blocked_version_closures"],
            summary["native_validation_status"],
        ),
        "",
        "## 模型矩阵",
        "",
        "| 模型 | 源/原生 universe | 最新 reqid | PASSORDER | 原生完成 | 绩效面板 | 安全 | 版本闭环 |",
        "|---|---:|---|---:|---|---|---|---|",
    ]
    for model in manifest["models"]:
        run = model["log_evidence"].get("latest_run") or {}
        closure = model["version_closure"]
        counts = "%s/%s" % (
            closure.get("source_universe_count") if closure.get("source_universe_count") is not None else "?",
            closure.get("native_run_universe_count") if closure.get("native_run_universe_count") is not None else "?",
        )
        lines.append(
            "| `%s` | `%s` | `%s` | `%s` | `%s` | `%s` | `%s` | `%s` |" % (
                model["qmt_model_name"],
                counts,
                run.get("reqid", "无"),
                run.get("backtest_passorder_calls_observed", 0),
                "是" if run.get("calc_backtest_index_count", 0) else "否",
                model["performance_panel"].get("status", "not_captured"),
                model["safety"]["status"],
                closure["status"],
            )
        )
    lines.extend(["", "## 版本闭环缺口", ""])
    if summary["verified_version_closures"] == 0:
        lines.append("当前 verified=0，没有模型形成完整版本闭环。")
        lines.append("")
    gaps = [model for model in manifest["models"] if model["version_closure"]["status"] != "verified"]
    if not gaps:
        lines.append("无。")
    else:
        for model in gaps:
            lines.append("- `%s`：%s。" % (
                model["qmt_model_name"], model["version_closure"]["reason"]
            ))
    lines.extend(["", "## HANDOFF 引用结论", ""])
    blocked_22 = [
        model for model in manifest["models"]
        if model["qmt_model_name"] in {"QMT_MULTIFACTOR_LIVE_SAFE", "QMT_ETF_ROTATION_LIVE_SAFE"}
        and model["version_closure"]["status"] != "verified"
    ]
    if blocked_22:
        lines.append(
            "当前 22 ETF `MULTIFACTOR` / `ETF_ROTATION` 尚未形成源码哈希→部署哈希→"
            "同版 QMT reqid→结果面板截图的闭环；现存原生日志对应 6 ETF 版本。"
        )
    lines.append(
        "日志中的 `PASSORDER` 仅表示 QMT 历史回测中观察到函数调用，不表示委托已受理，"
        "也不表示已经成交；只有已保存的绩效面板才标为原生绩效确认，任何本地或代理指标都不替代它。"
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--deployment-root", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--validation-root", type=Path)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    log_paths = sorted(args.log_dir.glob("XtClient_Formula_*.log"))
    validation_root = args.validation_root or args.source_root
    private_manifest = build_manifest(
        config, args.source_root, args.deployment_root, log_paths, validation_root
    )
    manifest = sanitize_manifest(private_manifest)
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(rendered, encoding="utf-8")
    args.markdown_out.write_text(render_markdown(manifest), encoding="utf-8")
    print(rendered, end="")
    return 1 if private_manifest["summary"]["safety_status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
