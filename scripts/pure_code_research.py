# coding: utf-8
"""Leakage-resistant pure-Python walk-forward research utilities.

This module is deliberately independent of QMT/xtquant.  It consumes cached
OHLCV dictionaries, evaluates signals formed at close ``t`` at the next open,
and reports data-quality/survivorship evidence alongside performance.  It is
research code only: no account is opened and no order is submitted.
"""

from __future__ import annotations

import concurrent.futures
import datetime as _datetime
import math
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

from qmt_backtest_research import (
    TransactionCosts,
    performance_metrics,
    run_weighted_portfolio,
)


@dataclass(frozen=True)
class DataQualityRules:
    """Thresholds for rejecting silently incomplete market data."""

    max_missing_rate: float = 0.01
    max_zero_volume_days: int = 5
    fail_on_zero_volume: bool = False
    minimum_rows: int = 3

    def __post_init__(self):
        if not 0 <= float(self.max_missing_rate) <= 1:
            raise ValueError("max_missing_rate must be between 0 and 1")
        if int(self.max_zero_volume_days) < 0 or int(self.minimum_rows) < 1:
            raise ValueError("data-quality counts must be non-negative/positive")


@dataclass(frozen=True)
class WalkForwardConfig:
    """Chronological rolling-fold layout.

    ``step_bars`` defaults to the test width, so OOS windows do not overlap.
    ``warmup_bars`` is extra history before each fold and is never included in
    reported train/validation/test metrics.
    """

    train_bars: int = 504
    validation_bars: int = 126
    test_bars: int = 126
    step_bars: int = 126
    warmup_bars: int = 0
    min_folds: int = 1

    def __post_init__(self):
        values = (self.train_bars, self.validation_bars, self.test_bars, self.step_bars)
        if any(int(value) < 1 for value in values):
            raise ValueError("walk-forward window sizes must be positive")
        if int(self.warmup_bars) < 0 or int(self.min_folds) < 1:
            raise ValueError("warmup_bars must be non-negative and min_folds positive")


def flatten_universe_metadata(universe: Mapping) -> dict[str, dict]:
    """Flatten ``etf_universe.json`` categories into a symbol metadata map."""
    result = {}
    if not isinstance(universe, Mapping):
        raise TypeError("universe must be a mapping")
    for category, entries in universe.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping) or not entry.get("symbol"):
                continue
            item = dict(entry)
            item.setdefault("category", category)
            result[str(item["symbol"])] = item
    return result


def _date(value) -> str:
    text = str(value)
    try:
        return _datetime.date.fromisoformat(text).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("bar date must be ISO YYYY-MM-DD: %r" % value) from exc


def _finite(value, field, symbol):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s has non-numeric %s" % (symbol, field)) from exc
    if not math.isfinite(number):
        raise ValueError("%s has non-finite %s" % (symbol, field))
    return number


def validate_bars(
    bars: Sequence[Mapping],
    symbol: str = "<unknown>",
    expected_dates: Iterable[str] | None = None,
    rules: DataQualityRules | None = None,
) -> dict:
    """Validate one symbol and return evidence instead of silently repairing it.

    Missing dates are measurable only when a reference trading-date calendar is
    supplied.  Zero volume is a warning by default because some public ETF
    caches carry valid prices but incomplete volume fields.
    """
    rules = rules or DataQualityRules()
    rows = list(bars or ())
    report = {
        "symbol": str(symbol),
        "rows": len(rows),
        "status": "pass",
        "issues": [],
        "warnings": [],
        "missing_rate": None,
        "missing_days": 0,
        "zero_volume_days": 0,
        "start": None,
        "end": None,
    }
    if len(rows) < rules.minimum_rows:
        report["status"] = "fail"
        report["issues"].append("fewer than minimum_rows")
        return report

    dates = []
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            report["status"] = "fail"
            report["issues"].append("row %d is not a mapping" % index)
            continue
        try:
            date = _date(row.get("date"))
            dates.append(date)
            if date in seen:
                report["status"] = "fail"
                report["issues"].append("duplicate date %s" % date)
            seen.add(date)
            values = {field: _finite(row.get(field), field, symbol) for field in ("open", "high", "low", "close")}
            if min(values.values()) <= 0:
                report["status"] = "fail"
                report["issues"].append("non-positive OHLC on %s" % date)
            if values["high"] < max(values["open"], values["close"]) or values["low"] > min(values["open"], values["close"]):
                report["status"] = "fail"
                report["issues"].append("OHLC range violation on %s" % date)
        except ValueError as exc:
            report["status"] = "fail"
            report["issues"].append(str(exc))

    if dates:
        report["start"], report["end"] = dates[0], dates[-1]
        if dates != sorted(dates):
            report["status"] = "fail"
            report["issues"].append("dates are not sorted")
    zero_volume = 0
    for row in rows:
        try:
            volume = _finite(row.get("volume", 0.0), "volume", symbol)
            zero_volume += volume <= 0
        except ValueError as exc:
            report["status"] = "fail"
            report["issues"].append(str(exc))
    report["zero_volume_days"] = zero_volume
    if zero_volume > rules.max_zero_volume_days:
        message = "zero-volume days=%d exceeds %d" % (zero_volume, rules.max_zero_volume_days)
        if rules.fail_on_zero_volume:
            report["status"] = "fail"
            report["issues"].append(message)
        else:
            report["warnings"].append(message)

    if expected_dates is not None:
        expected = {_date(value) for value in expected_dates}
        missing = expected.difference(seen)
        report["missing_days"] = len(missing)
        report["missing_rate"] = len(missing) / float(len(expected) or 1)
        if report["missing_rate"] > rules.max_missing_rate:
            report["status"] = "fail"
            report["issues"].append(
                "missing rate %.4f exceeds %.4f" % (report["missing_rate"], rules.max_missing_rate)
            )
    if report["status"] == "pass" and report["warnings"]:
        report["status"] = "warn"
    return report


def _copy_rows(rows):
    return [dict(row) for row in rows]


def align_bars(
    bars_by_symbol: Mapping[str, Sequence[Mapping]],
    benchmark_symbol: str | None = None,
    rules: DataQualityRules | None = None,
    strict: bool = True,
) -> tuple[dict[str, list[dict]], list[str], dict]:
    """Validate and date-align bars, retaining a quality report.

    The benchmark calendar is clipped to the common active interval before
    missing-rate checks.  Thus a newly listed ETF is recorded as limited
    history by survivorship diagnostics rather than being mislabeled as a
    missing-data failure.
    """
    if not bars_by_symbol:
        raise ValueError("bars_by_symbol is required")
    rules = rules or DataQualityRules()
    rows_by_symbol = {str(symbol): _copy_rows(rows) for symbol, rows in bars_by_symbol.items()}
    date_sets = {}
    for symbol, rows in rows_by_symbol.items():
        basic = validate_bars(rows, symbol, rules=rules)
        if strict and basic["status"] == "fail":
            raise ValueError("data quality failed for %s: %s" % (symbol, "; ".join(basic["issues"])))
        date_sets[symbol] = {_date(row["date"]) for row in rows}
    common = sorted(set.intersection(*(dates for dates in date_sets.values())))
    if len(common) < 3:
        raise ValueError("symbols must share at least three dated bars")

    benchmark = benchmark_symbol if benchmark_symbol in rows_by_symbol else next(iter(rows_by_symbol))
    expected = [date for date in sorted(date_sets[benchmark]) if common[0] <= date <= common[-1]]
    quality = {}
    failures = []
    for symbol, rows in rows_by_symbol.items():
        quality[symbol] = validate_bars(rows, symbol, expected_dates=expected, rules=rules)
        if quality[symbol]["status"] == "fail":
            failures.append(symbol)
    if strict and failures:
        raise ValueError("data quality failed for: %s" % ", ".join(failures))

    allowed = set(common)
    aligned = {
        symbol: sorted((row for row in rows if _date(row["date"]) in allowed), key=lambda row: _date(row["date"]))
        for symbol, rows in rows_by_symbol.items()
    }
    diagnostics = {
        "status": "fail" if failures else ("warn" if any(item["status"] == "warn" for item in quality.values()) else "pass"),
        "benchmark_calendar": benchmark,
        "common_bars": len(common),
        "start": common[0],
        "end": common[-1],
        "symbols": list(aligned),
        "quality": quality,
    }
    return aligned, common, diagnostics


def check_survivorship(
    bars_by_symbol: Mapping[str, Sequence[Mapping]],
    metadata: Mapping[str, Mapping] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    membership_history: Sequence[Mapping] | None = None,
) -> dict:
    """Check point-in-time listing boundaries and universe membership evidence.

    A current ETF list cannot prove historical availability.  Without a
    dated membership history this function deliberately returns ``unverified``
    rather than claiming survivorship safety.
    """
    metadata = metadata or {}
    symbols = list(bars_by_symbol)
    observed_start = min(_date(rows[0]["date"]) for rows in bars_by_symbol.values() if rows)
    observed_end = max(_date(rows[-1]["date"]) for rows in bars_by_symbol.values() if rows)
    start = _date(start_date or observed_start)
    end = _date(end_date or observed_end)
    issues, warnings, limited = [], [], []
    for symbol, rows in bars_by_symbol.items():
        item = metadata.get(symbol, {})
        if not item:
            warnings.append("missing metadata for %s" % symbol)
            continue
        first = _date(rows[0]["date"]) if rows else None
        last = _date(rows[-1]["date"]) if rows else None
        listed = item.get("listed_date") or item.get("inception_date")
        delisted = item.get("delisted_date")
        if listed:
            listed = _date(listed)
            if first and first < listed:
                issues.append("%s has bars before listed_date" % symbol)
            if listed > start:
                limited.append(symbol)
        else:
            warnings.append("missing listed_date for %s" % symbol)
        if delisted:
            delisted = _date(delisted)
            if last and last > delisted:
                issues.append("%s has bars after delisted_date" % symbol)

    history_ok = False
    if membership_history:
        snapshots = sorted(membership_history, key=lambda row: _date(row.get("effective_date", row.get("date"))))
        valid = []
        for row in snapshots:
            effective = row.get("effective_date", row.get("date"))
            members = row.get("symbols", row.get("members"))
            if effective is None or not isinstance(members, (list, tuple, set)):
                issues.append("invalid membership-history snapshot")
                continue
            valid.append((_date(effective), set(map(str, members))))
        if valid and valid[0][0] <= start and valid[-1][0] >= end:
            history_ok = True
            for effective, members in valid:
                if effective <= end and any(symbol not in members for symbol in symbols if symbol in metadata):
                    warnings.append("static symbol set not fully represented at %s" % effective)
        else:
            warnings.append("membership history does not cover the full backtest interval")
    else:
        warnings.append("dated membership history was not supplied")

    if issues:
        status = "fail"
    elif not history_ok:
        status = "unverified"
    else:
        status = "pass"
    return {
        "status": status,
        "backtest_start": start,
        "backtest_end": end,
        "symbols": symbols,
        "limited_history_symbols": limited,
        "issues": issues,
        "warnings": warnings,
        "membership_history_verified": history_ok,
    }


def market_regimes(
    benchmark_bars: Sequence[Mapping],
    short_window: int = 50,
    long_window: int = 200,
) -> dict[str, str]:
    """Classify each benchmark date as bull/neutral/bear/unknown."""
    if not 1 <= short_window < long_window:
        raise ValueError("short_window must be smaller than long_window")
    rows = sorted(benchmark_bars, key=lambda row: _date(row["date"]))
    closes = [float(row["close"]) for row in rows]
    result = {}
    for index, row in enumerate(rows):
        if index + 1 < long_window:
            result[_date(row["date"])] = "unknown"
            continue
        short = sum(closes[index - short_window + 1 : index + 1]) / short_window
        long = sum(closes[index - long_window + 1 : index + 1]) / long_window
        trailing = closes[index] / closes[index - short_window] - 1.0
        if short > long and trailing > 0:
            state = "bull"
        elif short < long and trailing < 0:
            state = "bear"
        else:
            state = "neutral"
        result[_date(row["date"])] = state
    return result


def _slice_metrics(result: Mapping, start: int, end: int, include_anchor: bool = True) -> dict:
    curve = list(result["equity_curve"])
    dates = list(result["equity_dates"])
    start = max(0, int(start))
    end = min(len(curve), int(end))
    left = max(0, start - 1) if include_anchor else start
    if end - left < 2:
        raise ValueError("a metric slice needs at least two equity observations")
    metrics = performance_metrics(curve[left:end])
    left_date, right_date = dates[left], dates[end - 1]
    metrics.update(
        {
            "start": left_date,
            "end": right_date,
            "orders": sum(left_date <= order["date"] <= right_date for order in result.get("orders", [])),
            "transaction_costs": sum(
                float(order.get("total_cost", 0.0))
                for order in result.get("orders", [])
                if left_date <= order["date"] <= right_date
            ),
        }
    )
    return metrics


def _fold_layout(size: int, config: WalkForwardConfig) -> list[dict]:
    folds = []
    cursor = 0
    required = config.train_bars + config.validation_bars + config.test_bars
    while cursor + required <= size:
        train_start = cursor
        train_end = train_start + config.train_bars
        validation_end = train_end + config.validation_bars
        test_end = validation_end + config.test_bars
        folds.append(
            {
                "train_start": train_start,
                "train_end": train_end,
                "validation_end": validation_end,
                "test_end": test_end,
                "context_start": max(0, train_start - config.warmup_bars),
            }
        )
        cursor += config.step_bars
    if len(folds) < config.min_folds:
        raise ValueError("not enough bars for requested walk-forward folds")
    return folds


def _selection_score(train: Mapping, validation: Mapping) -> float:
    """Use only train/validation evidence; test is intentionally absent."""
    return (
        min(train["total_return"], validation["total_return"])
        + 0.20 * (train["sharpe"] + validation["sharpe"]) / 2.0
        - 0.75 * max(train["max_drawdown"], validation["max_drawdown"])
    )


def _evaluate_candidate(payload):
    spec, target_factory, aligned, fold, costs, initial_capital, lot_size = payload
    segment = {
        symbol: rows[fold["context_start"] : fold["test_end"]]
        for symbol, rows in aligned.items()
    }
    result = run_weighted_portfolio(
        segment,
        target_factory(spec["params"]),
        initial_capital=initial_capital,
        fee_rate=costs.commission_rate,
        slippage_rate=costs.slippage_rate,
        lot_size=lot_size,
        cost_model=costs,
    )
    offset = fold["context_start"]
    train = _slice_metrics(result, fold["train_start"] - offset, fold["train_end"] - offset, include_anchor=False)
    validation = _slice_metrics(result, fold["train_end"] - offset, fold["validation_end"] - offset)
    test = _slice_metrics(result, fold["validation_end"] - offset, fold["test_end"] - offset)
    return {
        "id": str(spec["id"]),
        "params": dict(spec["params"]),
        "selection_score": _selection_score(train, validation),
        "train": train,
        "validation": validation,
        "test": test,
        "_result": result,
        "_offset": offset,
    }


def _stitch_oos(selected_folds: Sequence[Mapping], initial_capital: float) -> tuple[list[float], list[str]]:
    curve = [float(initial_capital)]
    dates = []
    for fold in selected_folds:
        result = fold["_result"]
        offset = fold["_offset"]
        start = fold["layout"]["validation_end"] - offset
        end = fold["layout"]["test_end"] - offset
        anchor = max(0, start - 1)
        values = result["equity_curve"]
        if not dates:
            dates.append(result["equity_dates"][anchor])
        base = values[anchor]
        previous = values[anchor]
        for index in range(start, end):
            date = result["equity_dates"][index]
            if dates and date <= dates[-1]:
                previous = values[index]
                continue
            curve.append(curve[-1] * values[index] / previous)
            dates.append(date)
            previous = values[index]
    return curve, dates


def _context_metrics(curve: Sequence[float], dates: Sequence[str], regimes: Mapping[str, str]) -> dict:
    grouped = {}
    for index in range(1, len(curve)):
        state = regimes.get(dates[index], "unknown")
        grouped.setdefault(state, []).append(float(curve[index]) / float(curve[index - 1]) - 1.0)
    output = {}
    for state, returns in grouped.items():
        values = [1.0]
        for value in returns:
            values.append(values[-1] * (1.0 + value))
        metrics = performance_metrics(values)
        metrics["days"] = len(returns)
        output[state] = metrics
    return output


def _industry_exposure(result: Mapping, bars_by_symbol: Mapping, metadata: Mapping[str, Mapping], start: int, end: int) -> dict:
    """Report gross price-return and turnover grouped by metadata sleeve."""
    dates = result["equity_dates"]
    positions = result.get("positions_curve") or []
    by_symbol = {
        symbol: {_date(row["date"]): float(row["close"]) for row in rows}
        for symbol, rows in bars_by_symbol.items()
    }
    returns = {}
    turnover = {}
    orders = {}
    for index in range(max(1, start), min(end, len(dates), len(positions))):
        date, previous_date = dates[index], dates[index - 1]
        previous_positions = positions[index - 1]
        notionals = {}
        pnl = {}
        for symbol, quantity in previous_positions.items():
            if quantity <= 0 or previous_date not in by_symbol.get(symbol, {}) or date not in by_symbol.get(symbol, {}):
                continue
            sleeve = str(metadata.get(symbol, {}).get("sleeve", metadata.get(symbol, {}).get("category", "unknown")))
            previous_price = by_symbol[symbol][previous_date]
            current_price = by_symbol[symbol][date]
            notionals[sleeve] = notionals.get(sleeve, 0.0) + quantity * previous_price
            pnl[sleeve] = pnl.get(sleeve, 0.0) + quantity * (current_price - previous_price)
        for sleeve, notional in notionals.items():
            returns.setdefault(sleeve, []).append(pnl[sleeve] / notional if notional else 0.0)
    for order in result.get("orders", []):
        sleeve = str(metadata.get(order["symbol"], {}).get("sleeve", metadata.get(order["symbol"], {}).get("category", "unknown")))
        orders[sleeve] = orders.get(sleeve, 0) + 1
        turnover[sleeve] = turnover.get(sleeve, 0.0) + float(order.get("notional", order.get("shares", 0) * order.get("price", 0)))
    output = {}
    for sleeve, values in returns.items():
        curve = [1.0]
        for value in values:
            curve.append(curve[-1] * (1.0 + value))
        output[sleeve] = {
            **performance_metrics(curve),
            "days_held": len(values),
            "orders": orders.get(sleeve, 0),
            "turnover_value": turnover.get(sleeve, 0.0),
            "return_basis": "gross price return while held; portfolio result is net of costs",
        }
    return output


def benchmark_report(
    bars_by_symbol: Mapping[str, Sequence[Mapping]],
    benchmark_symbol: str,
    dates: Sequence[str],
    initial_capital: float = 100000.0,
) -> dict:
    """Compute a passive, no-reselection benchmark over the OOS dates."""
    if benchmark_symbol not in bars_by_symbol:
        return {"symbol": benchmark_symbol, "status": "unavailable"}
    closes = {_date(row["date"]): float(row["close"]) for row in bars_by_symbol[benchmark_symbol]}
    observed = [(date, closes[date]) for date in dates if date in closes]
    if len(observed) < 2:
        return {"symbol": benchmark_symbol, "status": "unavailable", "reason": "insufficient aligned closes"}
    first = observed[0][1]
    curve = [initial_capital * close / first for _, close in observed]
    metrics = performance_metrics(curve)
    metrics.update({"start": observed[0][0], "end": observed[-1][0], "days": len(observed) - 1})
    return {"symbol": benchmark_symbol, "status": "pass", "metrics": metrics}


def run_walk_forward(
    bars_by_symbol: Mapping[str, Sequence[Mapping]],
    candidate_specs: Sequence[Mapping],
    target_factory: Callable[[Mapping], Callable],
    config: WalkForwardConfig | None = None,
    costs: TransactionCosts | None = None,
    benchmark_symbol: str | None = None,
    metadata: Mapping[str, Mapping] | None = None,
    membership_history: Sequence[Mapping] | None = None,
    quality_rules: DataQualityRules | None = None,
    initial_capital: float = 100000.0,
    lot_size: int = 100,
    workers: int = 1,
) -> dict:
    """Run rolling train/validation selection and locked OOS evaluation.

    Candidate evaluation is parallelized with a thread pool when ``workers``
    is greater than one.  Threads keep user-supplied pure-Python strategy
    factories usable on Windows without requiring pickleable closures.  The
    strategy itself is still isolated per candidate/fold by a fresh factory.
    """
    if not candidate_specs:
        raise ValueError("candidate_specs is required")
    if not callable(target_factory):
        raise TypeError("target_factory must be callable")
    config = config or WalkForwardConfig()
    costs = costs or TransactionCosts()
    if workers < 1 or lot_size < 1:
        raise ValueError("workers and lot_size must be positive")
    specs = []
    for index, raw in enumerate(candidate_specs):
        if not isinstance(raw, Mapping):
            raise TypeError("candidate specs must be mappings")
        params = raw.get("params", raw)
        specs.append({"id": str(raw.get("id", index)), "params": dict(params)})
    benchmark_symbol = benchmark_symbol or next(iter(bars_by_symbol))
    aligned, dates, quality = align_bars(
        bars_by_symbol, benchmark_symbol=benchmark_symbol, rules=quality_rules, strict=True
    )
    folds = _fold_layout(len(dates), config)
    metadata = metadata or {}
    survivorship = check_survivorship(
        aligned, metadata, start_date=dates[0], end_date=dates[-1], membership_history=membership_history
    )
    fold_reports = []
    for layout in folds:
        payloads = [
            (spec, target_factory, aligned, layout, costs, initial_capital, lot_size)
            for spec in specs
        ]
        if workers == 1:
            candidates = [_evaluate_candidate(payload) for payload in payloads]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                candidates = list(pool.map(_evaluate_candidate, payloads))
        candidates.sort(key=lambda row: (-row["selection_score"], row["id"]))
        selected = candidates[0]
        fold_reports.append(
            {
                "layout": {
                    **layout,
                    "train": [dates[layout["train_start"]], dates[layout["train_end"] - 1]],
                    "validation": [dates[layout["train_end"]], dates[layout["validation_end"] - 1]],
                    "test": [dates[layout["validation_end"]], dates[layout["test_end"] - 1]],
                },
                "selected": {
                    key: value for key, value in selected.items() if not key.startswith("_")
                },
                "industry_exposure": _industry_exposure(
                    selected["_result"], aligned, metadata,
                    layout["validation_end"] - layout["context_start"],
                    layout["test_end"] - layout["context_start"],
                ),
                "_result": selected["_result"],
                "_offset": selected["_offset"],
            }
        )

    oos_curve, oos_dates = _stitch_oos(fold_reports, initial_capital)
    regimes = market_regimes(aligned[benchmark_symbol])
    oos_metrics = performance_metrics(oos_curve)
    oos_metrics.update({"start": oos_dates[0], "end": oos_dates[-1], "days": len(oos_curve) - 1})
    benchmark = benchmark_report(aligned, benchmark_symbol, oos_dates, initial_capital)
    if benchmark.get("status") == "pass":
        benchmark_return = benchmark["metrics"]["total_return"]
        oos_metrics["active_return"] = oos_metrics["total_return"] - benchmark_return
        oos_metrics["relative_return"] = (1.0 + oos_metrics["total_return"]) / (1.0 + benchmark_return) - 1.0
    context = _context_metrics(oos_curve, oos_dates, regimes)
    public_folds = []
    for fold in fold_reports:
        public_folds.append(
            {
                "layout": fold["layout"],
                "selected": fold["selected"],
                "industry_exposure": fold["industry_exposure"],
            }
        )
    return {
        "methodology": {
            "engine": "pure_code",
            "execution": "signal at close t, next-open fill",
            "selection": "each fold ranks train+validation only; test is locked",
            "walk_forward": {
                "train_bars": config.train_bars,
                "validation_bars": config.validation_bars,
                "test_bars": config.test_bars,
                "step_bars": config.step_bars,
                "warmup_bars": config.warmup_bars,
                "folds": len(public_folds),
            },
            "transaction_costs": {
                "commission_rate": costs.commission_rate,
                "slippage_rate": costs.slippage_rate,
                "stamp_duty_rate": costs.stamp_duty_rate,
                "transfer_fee_rate": costs.transfer_fee_rate,
                "minimum_commission": costs.minimum_commission,
            },
            "parallel_workers": workers,
            "orders_sent": False,
            "accounts_connected": False,
        },
        "data": {**quality, "survivorship": survivorship},
        "benchmark": benchmark,
        "market_regimes": {
            "benchmark_symbol": benchmark_symbol,
            "counts": {state: sum(value == state for value in regimes.values()) for state in set(regimes.values())},
            "oos_metrics": context,
        },
        "oos": {"metrics": oos_metrics, "dates": oos_dates, "equity_curve": oos_curve},
        "folds": public_folds,
    }


__all__ = [
    "DataQualityRules",
    "TransactionCosts",
    "WalkForwardConfig",
    "align_bars",
    "benchmark_report",
    "check_survivorship",
    "flatten_universe_metadata",
    "market_regimes",
    "run_walk_forward",
    "validate_bars",
]
