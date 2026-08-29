# coding: utf-8
"""Small, reproducible research backtester for QMT's local DAT cache.

It intentionally executes a signal generated on bar ``t`` at bar ``t+1``'s
open, so a backtest cannot use the same close that created the signal.
"""

import datetime as _datetime
import math
import os
import struct
from dataclasses import dataclass


RECORD_SIZE = 64
HEADER_SIZE = 8


@dataclass(frozen=True)
class TransactionCosts:
    """Explicit, auditable transaction-cost assumptions.

    ``commission_rate`` applies to both buys and sells.  ``stamp_duty_rate``
    applies to sells only (set it to zero for ETF research when appropriate).
    The model is intentionally a pure calculation: it has no broker or QMT
    dependency and never submits an order.
    """

    commission_rate: float = 0.0003
    slippage_rate: float = 0.0005
    stamp_duty_rate: float = 0.0
    transfer_fee_rate: float = 0.0
    minimum_commission: float = 0.0

    def __post_init__(self):
        values = (
            self.commission_rate,
            self.slippage_rate,
            self.stamp_duty_rate,
            self.transfer_fee_rate,
            self.minimum_commission,
        )
        if any(not math.isfinite(float(value)) or float(value) < 0 for value in values):
            raise ValueError("transaction-cost assumptions must be finite and non-negative")

    def execution_price(self, open_price, side):
        """Return the adverse-slippage execution price for a side."""
        price = float(open_price)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("open price must be a positive finite number")
        if side == "BUY":
            return price * (1.0 + self.slippage_rate)
        if side == "SELL":
            return price * (1.0 - self.slippage_rate)
        raise ValueError("side must be BUY or SELL")

    def charges(self, notional, side):
        """Return a transparent fee breakdown for one fill."""
        value = float(notional)
        if not math.isfinite(value) or value < 0:
            raise ValueError("notional must be finite and non-negative")
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        commission = max(self.minimum_commission, value * self.commission_rate) if value else 0.0
        stamp_duty = value * self.stamp_duty_rate if side == "SELL" else 0.0
        transfer_fee = value * self.transfer_fee_rate
        return {
            "commission": commission,
            "stamp_duty": stamp_duty,
            "transfer_fee": transfer_fee,
            "total_cost": commission + stamp_duty + transfer_fee,
        }


def decode_qmt_dat(blob):
    """Decode QMT daily DAT records into plain dictionaries.

    QMT stores timestamp/open/high/low/close as uint32 values; prices and
    indices use a scale of 1000.  The seventh uint32 is the share volume.
    """
    if len(blob) < HEADER_SIZE or (len(blob) - HEADER_SIZE) % RECORD_SIZE:
        raise ValueError("invalid QMT DAT length")
    bars = []
    for offset in range(HEADER_SIZE, len(blob), RECORD_SIZE):
        values = struct.unpack("<16I", blob[offset:offset + RECORD_SIZE])
        timestamp, open_value, high_value, low_value, close_value = values[:5]
        if timestamp <= 0:
            continue
        bars.append(
            {
                "date": _datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d"),
                "timestamp": timestamp,
                "open": open_value / 1000.0,
                "high": high_value / 1000.0,
                "low": low_value / 1000.0,
                "close": close_value / 1000.0,
                "volume": values[6],
            }
        )
    return bars


def load_qmt_daily_data(data_dir, symbol):
    """Load one ``SH``/``SZ`` symbol from a QMT ``datadir``."""
    if not symbol or "." not in symbol:
        raise ValueError("symbol must include exchange suffix, e.g. 510300.SH")
    code, exchange = symbol.split(".", 1)
    path = os.path.join(str(data_dir), exchange.upper(), "86400", code + ".DAT")
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    with open(path, "rb") as handle:
        return decode_qmt_dat(handle.read())


def performance_metrics(equity_curve, periods_per_year=252):
    values = [float(value) for value in equity_curve if value is not None]
    if not values or any(value <= 0 for value in values):
        raise ValueError("equity_curve must contain positive values")
    total_return = values[-1] / values[0] - 1.0
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1.0)
    returns = [right / left - 1.0 for left, right in zip(values[:-1], values[1:]) if left > 0]
    average = sum(returns) / float(len(returns)) if returns else 0.0
    variance = sum((value - average) ** 2 for value in returns) / float(len(returns)) if len(returns) > 1 else 0.0
    sharpe = (average / math.sqrt(variance) * math.sqrt(periods_per_year)) if variance > 0 else 0.0
    years = max((len(values) - 1) / float(periods_per_year), 1.0 / periods_per_year)
    cagr = (values[-1] / values[0]) ** (1.0 / years) - 1.0
    return {
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": abs(max_drawdown),
        "sharpe": sharpe,
    }


def run_single_asset(
    bars,
    signal_fn,
    initial_capital=100000.0,
    allocation=1.0,
    fee_rate=0.0003,
    slippage_rate=0.0005,
    lot_size=100,
):
    """Run a long-only strategy over OHLCV bars with next-open execution."""
    if len(bars) < 3:
        raise ValueError("at least three bars are required")
    if not 0 < allocation <= 1:
        raise ValueError("allocation must be in (0, 1]")

    cash = float(initial_capital)
    shares = 0
    open_trade = None
    trades = []
    equity_curve = []
    equity_dates = []

    for index, bar in enumerate(bars):
        mark = cash + shares * float(bar["close"])
        equity_curve.append(mark)
        equity_dates.append(bar.get("date", str(index)))
        if index >= len(bars) - 1:
            continue

        history = bars[: index + 1]
        want_long = bool(signal_fn(history))
        next_bar = bars[index + 1]

        if want_long and shares == 0:
            execution_price = float(next_bar["open"]) * (1.0 + slippage_rate)
            budget = cash * allocation
            shares = int(budget / (execution_price * (1.0 + fee_rate)) / lot_size) * lot_size
            if shares > 0:
                total_cost = shares * execution_price * (1.0 + fee_rate)
                cash -= total_cost
                open_trade = {
                    "entry_date": next_bar.get("date", str(index + 1)),
                    "entry_price": execution_price,
                    "shares": shares,
                }
        elif not want_long and shares > 0:
            execution_price = float(next_bar["open"]) * (1.0 - slippage_rate)
            proceeds = shares * execution_price * (1.0 - fee_rate)
            cash += proceeds
            if open_trade is not None:
                closed = dict(open_trade)
                closed.update(
                    {
                        "exit_date": next_bar.get("date", str(index + 1)),
                        "exit_price": execution_price,
                        "pnl": proceeds - open_trade["shares"] * open_trade["entry_price"] * (1.0 + fee_rate),
                    }
                )
                trades.append(closed)
            shares = 0
            open_trade = None

    final_equity = cash + shares * float(bars[-1]["close"])
    if open_trade is not None:
        marked = dict(open_trade)
        marked.update({"exit_date": None, "exit_price": None, "pnl": None, "open": True})
        trades.append(marked)
    return {
        "initial_capital": initial_capital,
        "final_equity": final_equity,
        "cash": cash,
        "shares": shares,
        "trades": trades,
        "equity_curve": equity_curve + ([final_equity] if not equity_curve or equity_curve[-1] != final_equity else []),
        "equity_dates": equity_dates,
        "metrics": performance_metrics(equity_curve + ([final_equity] if not equity_curve or equity_curve[-1] != final_equity else [])),
    }


def run_rotation_portfolio(
    bars_by_symbol,
    target_fn,
    initial_capital=100000.0,
    allocation=1.0,
    fee_rate=0.0003,
    slippage_rate=0.0005,
    lot_size=100,
):
    """Run a one-position rotation portfolio with next-open execution.

    ``target_fn`` receives a date-aligned history mapping and returns one
    symbol or ``None``.  The target is evaluated using bar ``t`` data and any
    switch is executed at bar ``t+1``'s open, preventing close-to-close
    look-ahead.  The implementation is intentionally dependency-free so it
    can also be used to audit QMT-exported bars offline.
    """
    if not bars_by_symbol or not callable(target_fn):
        raise ValueError("bars_by_symbol and target_fn are required")
    if not 0 < allocation <= 1:
        raise ValueError("allocation must be in (0, 1]")
    symbols = list(bars_by_symbol)
    date_sets = []
    for symbol in symbols:
        rows = bars_by_symbol[symbol]
        if len(rows) < 3:
            raise ValueError("each symbol needs at least three bars")
        date_sets.append(set(row.get("date") for row in rows if row.get("date") is not None))
    common_dates = sorted(set.intersection(*date_sets))
    if len(common_dates) < 3:
        raise ValueError("symbols must share at least three dated bars")

    aligned = {}
    for symbol in symbols:
        by_date = {row.get("date"): row for row in bars_by_symbol[symbol]}
        aligned[symbol] = [by_date[date] for date in common_dates]

    cash = float(initial_capital)
    current_symbol = None
    shares = 0
    open_trade = None
    trades = []
    equity_curve = []
    equity_dates = []

    for index, date in enumerate(common_dates):
        mark = cash
        if current_symbol is not None:
            mark += shares * float(aligned[current_symbol][index]["close"])
        equity_curve.append(mark)
        equity_dates.append(date)
        if index >= len(common_dates) - 1:
            continue

        history = {symbol: aligned[symbol][: index + 1] for symbol in symbols}
        target = target_fn(history)
        if target not in aligned:
            target = None
        next_index = index + 1

        if current_symbol != target:
            if current_symbol is not None and shares > 0:
                exit_bar = aligned[current_symbol][next_index]
                exit_price = float(exit_bar["open"]) * (1.0 - slippage_rate)
                proceeds = shares * exit_price * (1.0 - fee_rate)
                cash += proceeds
                if open_trade is not None:
                    closed = dict(open_trade)
                    closed.update(
                        {
                            "exit_date": exit_bar.get("date", str(next_index)),
                            "exit_price": exit_price,
                            "pnl": proceeds
                            - open_trade["shares"] * open_trade["entry_price"] * (1.0 + fee_rate),
                        }
                    )
                    trades.append(closed)
                current_symbol = None
                shares = 0
                open_trade = None

            if target is not None:
                entry_bar = aligned[target][next_index]
                entry_price = float(entry_bar["open"]) * (1.0 + slippage_rate)
                budget = cash * allocation
                shares = int(budget / (entry_price * (1.0 + fee_rate)) / lot_size) * lot_size
                if shares > 0:
                    total_cost = shares * entry_price * (1.0 + fee_rate)
                    cash -= total_cost
                    current_symbol = target
                    open_trade = {
                        "symbol": target,
                        "entry_date": entry_bar.get("date", str(next_index)),
                        "entry_price": entry_price,
                        "shares": shares,
                    }

    final_equity = cash
    if current_symbol is not None:
        final_equity += shares * float(aligned[current_symbol][-1]["close"])
    if open_trade is not None:
        marked = dict(open_trade)
        marked.update({"exit_date": None, "exit_price": None, "pnl": None, "open": True})
        trades.append(marked)
    full_curve = equity_curve + ([final_equity] if not equity_curve or equity_curve[-1] != final_equity else [])
    return {
        "initial_capital": initial_capital,
        "final_equity": final_equity,
        "cash": cash,
        "symbol": current_symbol,
        "shares": shares,
        "trades": trades,
        "equity_curve": full_curve,
        "equity_dates": equity_dates,
        "metrics": performance_metrics(full_curve),
    }


def run_weighted_portfolio(
    bars_by_symbol,
    target_weights_fn,
    initial_capital=100000.0,
    fee_rate=0.0003,
    slippage_rate=0.0005,
    lot_size=100,
    cost_model=None,
):
    """Backtest long-only target weights with next-open execution.

    ``target_weights_fn`` receives history through bar ``t`` and returns a
    ``{symbol: weight}`` mapping.  Sells and buys are executed at bar
    ``t+1``'s open, sells first, using whole lots.  Weights must be finite,
    non-negative and sum to at most one.  Cash is the residual asset.
    """
    if not bars_by_symbol or not callable(target_weights_fn):
        raise ValueError("bars_by_symbol and target_weights_fn are required")
    if lot_size <= 0:
        raise ValueError("lot_size must be positive")

    symbols = list(bars_by_symbol)
    date_sets = []
    for symbol in symbols:
        rows = bars_by_symbol[symbol]
        if len(rows) < 3:
            raise ValueError("each symbol needs at least three bars")
        date_sets.append(set(row.get("date") for row in rows if row.get("date") is not None))
    common_dates = sorted(set.intersection(*date_sets))
    if len(common_dates) < 3:
        raise ValueError("symbols must share at least three dated bars")

    aligned = {}
    for symbol in symbols:
        by_date = {row.get("date"): row for row in bars_by_symbol[symbol]}
        aligned[symbol] = [by_date[date] for date in common_dates]

    costs = cost_model or TransactionCosts(
        commission_rate=fee_rate,
        slippage_rate=slippage_rate,
    )
    if not isinstance(costs, TransactionCosts):
        raise TypeError("cost_model must be a TransactionCosts instance")

    cash = float(initial_capital)
    shares = {symbol: 0 for symbol in symbols}
    orders = []
    equity_curve = []
    equity_dates = []
    positions_curve = []
    turnover_value = 0.0
    transaction_costs = 0.0

    for index, date in enumerate(common_dates):
        mark = cash + sum(
            shares[symbol] * float(aligned[symbol][index]["close"])
            for symbol in symbols
        )
        equity_curve.append(mark)
        equity_dates.append(date)
        positions_curve.append(dict(shares))
        if index >= len(common_dates) - 1:
            continue

        history = {symbol: aligned[symbol][: index + 1] for symbol in symbols}
        requested = target_weights_fn(history) or {}
        weights = {}
        for symbol, raw_weight in requested.items():
            if symbol not in aligned:
                continue
            weight = float(raw_weight)
            if not math.isfinite(weight) or weight < 0:
                raise ValueError("target weights must be finite and non-negative")
            if weight > 0:
                weights[symbol] = weight
        if sum(weights.values()) > 1.0000001:
            raise ValueError("target weights must sum to at most one")

        next_index = index + 1
        raw_opens = {
            symbol: float(aligned[symbol][next_index]["open"])
            for symbol in symbols
        }
        pretrade_equity = cash + sum(
            shares[symbol] * raw_opens[symbol] for symbol in symbols
        )
        target_shares = {}
        for symbol in symbols:
            buy_price = costs.execution_price(raw_opens[symbol], "BUY")
            budget = pretrade_equity * weights.get(symbol, 0.0)
            # Include all buy-side charges in affordability.  A minimum
            # commission is fixed per order and therefore conservatively
            # reserved before rounding to whole lots.
            unit_cost = buy_price * (1.0 + costs.commission_rate + costs.transfer_fee_rate)
            if costs.minimum_commission > 0:
                unit_cost += costs.minimum_commission / max(lot_size, 1)
            target_shares[symbol] = int(
                budget / unit_cost / lot_size
            ) * lot_size

        for symbol in symbols:
            difference = target_shares[symbol] - shares[symbol]
            if difference >= 0:
                continue
            quantity = -difference
            execution_price = costs.execution_price(raw_opens[symbol], "SELL")
            notional = quantity * execution_price
            charge = costs.charges(notional, "SELL")
            proceeds = notional - charge["total_cost"]
            cash += proceeds
            shares[symbol] -= quantity
            turnover_value += notional
            transaction_costs += charge["total_cost"]
            orders.append(
                {
                    "date": aligned[symbol][next_index].get("date", str(next_index)),
                    "symbol": symbol,
                    "side": "SELL",
                    "shares": quantity,
                    "price": execution_price,
                    "notional": notional,
                    **charge,
                }
            )

        buy_candidates = []
        for symbol in symbols:
            difference = target_shares[symbol] - shares[symbol]
            if difference > 0:
                buy_candidates.append((weights.get(symbol, 0.0), symbol, difference))
        buy_candidates.sort(reverse=True)
        for _, symbol, requested_quantity in buy_candidates:
            execution_price = costs.execution_price(raw_opens[symbol], "BUY")
            unit_cost = execution_price * (1.0 + costs.commission_rate + costs.transfer_fee_rate)
            affordable = int(cash / unit_cost / lot_size) * lot_size
            quantity = min(requested_quantity, affordable)
            if quantity <= 0:
                continue
            notional = quantity * execution_price
            charge = costs.charges(notional, "BUY")
            cash -= notional + charge["total_cost"]
            shares[symbol] += quantity
            turnover_value += notional
            transaction_costs += charge["total_cost"]
            orders.append(
                {
                    "date": aligned[symbol][next_index].get("date", str(next_index)),
                    "symbol": symbol,
                    "side": "BUY",
                    "shares": quantity,
                    "price": execution_price,
                    "notional": notional,
                    **charge,
                }
            )

    final_equity = cash + sum(
        shares[symbol] * float(aligned[symbol][-1]["close"])
        for symbol in symbols
    )
    full_curve = equity_curve + (
        [final_equity] if not equity_curve or equity_curve[-1] != final_equity else []
    )
    result = {
        "initial_capital": initial_capital,
        "final_equity": final_equity,
        "cash": cash,
        "shares": {symbol: value for symbol, value in shares.items() if value > 0},
        "orders": orders,
        "trades": orders,
        "turnover_value": turnover_value,
        "turnover_ratio": turnover_value / float(initial_capital),
        "transaction_costs": transaction_costs,
        "cost_model": {
            "commission_rate": costs.commission_rate,
            "slippage_rate": costs.slippage_rate,
            "stamp_duty_rate": costs.stamp_duty_rate,
            "transfer_fee_rate": costs.transfer_fee_rate,
            "minimum_commission": costs.minimum_commission,
        },
        "equity_curve": full_curve,
        "equity_dates": equity_dates,
        "positions_curve": positions_curve,
        "metrics": performance_metrics(full_curve),
    }
    result["metrics"]["turnover_ratio"] = result["turnover_ratio"]
    result["metrics"]["orders"] = len(orders)
    return result


def buy_and_hold_signal(history):
    return len(history) >= 1
