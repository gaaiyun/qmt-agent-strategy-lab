# coding: utf-8
"""Parallel, config-driven multi-horizon research for QMT candidates.

The runner is intentionally independent of the QMT GUI.  It evaluates a
small, auditable parameter grid with next-open execution, locked chronological
splits and rolling folds.  QMT is used later only to compile and reproduce the
short-list; this module never imports an account or sends an order.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
from pathlib import Path

# Keep the examples importable when the CLI is launched from any working
# directory (including a clean CI checkout).
REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))

from qmt_backtest_research import performance_metrics, run_weighted_portfolio
from qmt_etf_rotation_live import rotation_score
from qmt_multifactor_live import score_factors


DEFENSIVE_SYMBOL = "511010.SH"


def _load_cache(cache_path):
    """Load every valid symbol and align it to the defensive ETF dates."""
    raw = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    if DEFENSIVE_SYMBOL not in raw:
        raise RuntimeError("cache is missing defensive ETF %s" % DEFENSIVE_SYMBOL)
    symbols = tuple(symbol for symbol, rows in raw.items() if rows and symbol != DEFENSIVE_SYMBOL)
    if len(symbols) < 3:
        raise RuntimeError("at least three risk ETFs are required")
    dates = set(row["date"] for row in raw[DEFENSIVE_SYMBOL])
    for symbol in symbols:
        dates.intersection_update(row["date"] for row in raw[symbol])
    common = sorted(dates)
    if len(common) < 300:
        raise RuntimeError("common ETF history is too short")
    allowed = set(common)
    bars = {
        symbol: [row for row in raw[symbol] if row["date"] in allowed]
        for symbol in symbols + (DEFENSIVE_SYMBOL,)
    }
    return bars, common, symbols


def _inverse_volatility(items, gross_weight, cap):
    """Allocate a gross risk budget inversely to realized volatility."""
    if not items:
        return {}
    raw = {item["symbol"]: 1.0 / max(item.get("volatility", 0.08), 0.08) for item in items}
    total = sum(raw.values())
    return {symbol: min(cap, gross_weight * value / total) for symbol, value in raw.items()}


def _factory(family, params, risk_symbols):
    """Build a stateful target function for an arbitrary configured universe."""
    state = {"last": -10**9, "weights": {}, "risk": None}

    def target(history):
        count = len(history[risk_symbols[0]])
        if count - state["last"] < params["rebalance_bars"]:
            return state["weights"]
        if count < max(params["long_window"] + 1, 101):
            return {}
        ranked = []
        for symbol in risk_symbols:
            rows = history.get(symbol, [])
            if family == "etf_rotation":
                diagnostics = rotation_score(
                    [row["close"] for row in rows],
                    short_window=params["short_window"],
                    mid_window=params["mid_window"],
                    long_window=params["long_window"],
                )
            else:
                diagnostics = score_factors(
                    [row["close"] for row in rows],
                    [row.get("volume", 0.0) for row in rows],
                    short_window=params["short_window"],
                    mid_window=params["mid_window"],
                    long_window=params["long_window"],
                )
            if diagnostics and diagnostics.get("score") is not None and diagnostics["score"] >= params["min_score"]:
                if params["require_trend"] and not diagnostics.get("trend"):
                    continue
                item = dict(diagnostics)
                item["symbol"] = symbol
                ranked.append(item)
        ranked.sort(key=lambda item: item["score"], reverse=True)
        if family == "etf_rotation":
            chosen = ranked[0] if ranked else None
            if chosen:
                current = next((item for item in ranked if item["symbol"] == state["risk"]), None)
                if current and chosen["score"] < current["score"] + params["switch_buffer"]:
                    chosen = current
            weights = {}
            if chosen:
                risk_weight = min(params["max_risk_weight"], params["volatility_target"] / max(chosen.get("volatility", 0.08), 0.08))
                if risk_weight >= params["min_risk_weight"]:
                    weights[chosen["symbol"]] = risk_weight
                    state["risk"] = chosen["symbol"]
        else:
            selected = ranked[: params["max_positions"]]
            average_vol = sum(item.get("volatility", 0.08) for item in selected) / max(len(selected), 1)
            gross = min(params["max_risk_weight"], params["volatility_target"] / max(average_vol, 0.08)) if selected else 0.0
            weights = _inverse_volatility(selected, gross, params["max_position_weight"])
        weights[DEFENSIVE_SYMBOL] = max(0.0, 1.0 - sum(weights.values()))
        state["last"] = count
        state["weights"] = weights
        return weights

    return target


def _grid():
    """Return a deliberately small grid suitable for repeated CI runs."""
    rows = []
    for short, mid, long in ((5, 15, 60), (10, 20, 80), (15, 30, 120)):
        rows.append(
            {
                "short_window": short,
                "mid_window": mid,
                "long_window": long,
                "min_score": 0.0,
                "rebalance_bars": 10,
                "volatility_target": 0.12,
                "max_risk_weight": 0.75,
                "min_risk_weight": 0.25,
                "switch_buffer": 0.02,
                "max_positions": 2,
                "require_trend": True,
                "max_position_weight": 0.50,
            }
        )
    return rows


def _slice_metrics(result, start, end):
    """Compute metrics on a fixed curve interval without reselecting it."""
    curve = result["equity_curve"]
    dates = result["equity_dates"]
    start = max(0, min(int(start), len(curve) - 2))
    end = max(start + 2, min(int(end), len(curve)))
    metrics = performance_metrics(curve[start:end])
    left, right = dates[start], dates[end - 1]
    metrics["orders"] = sum(left <= row["date"] <= right for row in result["orders"])
    metrics["start"] = left
    metrics["end"] = right
    return metrics


def _folds(size):
    """Return overlapping 1y/2y/3y windows and a final OOS window."""
    windows = []
    for length, label in ((252, "1y"), (504, "2y"), (756, "3y")):
        if size >= length + 2:
            windows.append((label, size - length, size))
    if size >= 504:
        windows.append(("early", 0, min(504, size)))
    return windows


def _candidate_worker(payload):
    """Evaluate one family/parameter combination in a worker process."""
    family = payload["family"]
    bars = payload["bars"]
    params = payload["params"]
    result = run_weighted_portfolio(
        bars,
        _factory(family, params, payload["risk_symbols"]),
        initial_capital=100000.0,
        fee_rate=payload["fee_rate"],
        slippage_rate=payload["slippage_rate"],
        lot_size=100,
    )
    size = len(result["equity_curve"])
    train_end = int(size * 0.50)
    validation_end = int(size * 0.75)
    train = _slice_metrics(result, 0, train_end)
    validation = _slice_metrics(result, train_end, validation_end)
    oos = _slice_metrics(result, validation_end, size)
    folds = {
        label: _slice_metrics(result, start, end)
        for label, start, end in _folds(size)
    }
    worst_return = min(train["total_return"], validation["total_return"])
    selection_score = (
        worst_return
        + 0.20 * (train["sharpe"] + validation["sharpe"]) / 2.0
        - 0.75 * max(train["max_drawdown"], validation["max_drawdown"])
    )
    return {
        "family": family,
        "params": params,
        "selection_score": selection_score,
        "train": train,
        "validation": validation,
        "oos": oos,
        "folds": folds,
        "full": result["metrics"],
    }


def run_batch(cache_path, workers=None, fee_rate=0.0003, slippage_rate=0.0005):
    """Run both families concurrently and return a machine-readable report."""
    bars, dates, risk_symbols = _load_cache(cache_path)
    families = ("etf_rotation", "etf_multifactor")
    payloads = [
        {
            "family": family,
            "params": params,
            "bars": bars,
            "risk_symbols": risk_symbols,
            "fee_rate": fee_rate,
            "slippage_rate": slippage_rate,
        }
        for family in families
        for params in _grid()
    ]
    max_workers = workers or min(8, max(1, (os.cpu_count() or 2) - 1))
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as pool:
        rows = list(pool.map(_candidate_worker, payloads))
    grouped = {}
    for family in families:
        family_rows = sorted(
            (row for row in rows if row["family"] == family),
            key=lambda row: row["selection_score"],
            reverse=True,
        )
        grouped[family] = {
            "selected_pretest": family_rows[0],
            "candidates": family_rows,
            "positive_oos": sum(row["oos"]["total_return"] > 0 for row in family_rows),
        }
    return {
        "methodology": {
            "source": "cached adjusted OHLCV",
            "bars": len(dates),
            "start": dates[0],
            "end": dates[-1],
            "symbols": list(bars),
            "fee_rate": fee_rate,
            "slippage_rate": slippage_rate,
            "execution": "signal at close t, next-open fill",
            "selection": "train/validation only; OOS and folds locked",
            "workers": max_workers,
            "orders_sent": False,
        },
        "families": grouped,
    }


def _pct(value):
    return "%.2f%%" % (100.0 * float(value))


def render_markdown(report):
    """Render a concise report that keeps evidence tiers explicit."""
    method = report["methodology"]
    lines = [
        "# 并行多周期 QMT 候选研究",
        "",
        "- 数据：%d 个共同交易日，%s 至 %s；%d 个 ETF。" % (
            method["bars"], method["start"], method["end"], len(method["symbols"])
        ),
        "- 执行：收盘 t 生成信号，下一交易日开盘成交；费率 %.4f、滑点 %.4f。" % (
            method["fee_rate"], method["slippage_rate"]
        ),
        "- 选择：训练 50%%、验证 25%%、OOS 25%%；候选按训练/验证一致性排序，OOS 不参与选参。",
        "- 并行进程：%d；本脚本未连接账户、未发送委托。" % method["workers"],
        "",
        "| 家族 | 训练收益 | 验证收益 | OOS 收益 | OOS 回撤 | OOS Sharpe | 正 OOS 候选 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for family, data in report["families"].items():
        row = data["selected_pretest"]
        lines.append(
            "| %s | %s | %s | %s | %s | %.2f | %d/%d |"
            % (
                family,
                _pct(row["train"]["total_return"]),
                _pct(row["validation"]["total_return"]),
                _pct(row["oos"]["total_return"]),
                _pct(row["oos"]["max_drawdown"]),
                row["oos"]["sharpe"],
                data["positive_oos"],
                len(data["candidates"]),
            )
        )
        lines.extend(
            [
                "",
                "## %s 固化候选（仅供 QMT 原生复核）" % family,
                "",
                "参数：`%s`" % json.dumps(row["params"], ensure_ascii=False, sort_keys=True),
                "",
                "| 窗口 | 收益 | 回撤 | Sharpe | 订单数 |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for label, metrics in row["folds"].items():
            lines.append(
                "| %s | %s | %s | %.2f | %d |"
                % (
                    label,
                    _pct(metrics["total_return"]),
                    _pct(metrics["max_drawdown"]),
                    metrics["sharpe"],
                    metrics["orders"],
                )
            )
    lines.extend(
        [
            "",
            "## 证据边界",
            "",
            "这是本地代理回测。QMT 编译/回测面板、模拟账户成交与本报告分别记录；代理结果不能替代券商 QMT 原生绩效。",
            "",
        ]
    )
    return "\n".join(lines)


def main():
    """Parse CLI arguments and write JSON/Markdown artifacts."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", default=str(Path(__file__).with_name("yahoo_etf_cache.json")))
    parser.add_argument("--output-dir", default=str(Path(__file__).parent))
    parser.add_argument("--workers", type=int)
    args = parser.parse_args()
    report = run_batch(args.cache, workers=args.workers)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "batch_research_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "batch_research_report.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    print(render_markdown(report))


if __name__ == "__main__":
    main()
