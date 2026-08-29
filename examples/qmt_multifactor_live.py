# coding: gbk
"""QMT multi-factor long-only strategy with a guarded live-order path.

The strategy is deliberately dependency-free so that it runs in QMT's bundled
Python 3.6 runtime.  It combines momentum, trend, breadth, liquidity and
volatility/drawdown controls.  ``ENABLE_ORDERS`` stays False by default;
backtest orders are separately gated by QMT's ``trade_mode``.
"""

import math


ENABLE_ORDERS = False
BACKTEST_ORDERS = True
LIVE_CONFIRM_TOKEN = ""
# Leave this empty in distributed code.  A live account must be explicitly
# allow-listed in the local deployment before any live order can pass.
VALID_ACCOUNTS = ()

# Cross-sectional ETF sleeves.  This is an ETF multi-factor model (price,
# trend, breadth, liquidity, volatility and drawdown), not a stock-level
# fundamentals model.  Add stock fundamentals only through a versioned data
# adapter; never substitute unavailable fields with future data.
RISK_UNIVERSE = (
    "510300.SH", "510500.SH", "159915.SZ", "512100.SH", "588000.SH",
    "512890.SH", "512480.SH", "512930.SH", "515230.SH", "512170.SH",
    "159928.SZ", "512800.SH", "512880.SH", "512400.SH", "515220.SH",
    "515790.SH", "512660.SH", "513100.SH", "513500.SH", "513050.SH",
    "518880.SH",
)
DEFENSIVE_SYMBOL = "511010.SH"
UNIVERSE = RISK_UNIVERSE + (DEFENSIVE_SYMBOL,)
MAX_POSITIONS = 3
MODEL_CAPITAL = 100000.0
REBALANCE_EVERY_BARS = 10
SHORT_WINDOW = 10
MID_WINDOW = 15
LONG_WINDOW = 40
MIN_SCORE = 0.0
REQUIRE_TREND = False
VOLATILITY_TARGET = 0.12
MAX_RISK_WEIGHT = 0.90
MAX_POSITION_WEIGHT = 0.35
MIN_REBALANCE_VALUE = 2000.0
MAX_ORDER_VALUE = 50000.0
MAX_ORDERS_PER_BAR = 8
STOP_LOSS = 0.10
TAKE_PROFIT = 0.35
TRAILING_STOP = 0.12
MIN_HISTORY = LONG_WINDOW + 1
ORDER_LOT = 100


class RuntimeState(object):
    pass


g = RuntimeState()


def _clean(values):
    """Return finite positive floats and discard malformed market values."""
    result = []
    for value in values or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and math.isfinite(number):
            result.append(number)
    return result


def _clip(value, lower=-1.0, upper=1.0):
    """Clamp a numeric value to a closed interval."""
    return max(lower, min(upper, float(value)))


def _mean(values):
    """Return the arithmetic mean, using zero for an empty sequence."""
    return sum(values) / float(len(values)) if values else 0.0


def _std(values):
    """Return population standard deviation for deterministic factor scores."""
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / float(len(values)))


def _sma(values, window):
    """Return a trailing simple moving average or None if history is short."""
    return _mean(values[-window:]) if len(values) >= window else None


def _return(values, window):
    """Return the trailing simple return over ``window`` bars."""
    if len(values) <= window or values[-window - 1] <= 0:
        return None
    return values[-1] / values[-window - 1] - 1.0


def _annualized_volatility(values, window=20):
    """Estimate annualized close-to-close volatility with 252 trading days."""
    if len(values) < window + 1:
        return None
    returns = []
    for left, right in zip(values[-window - 1:-1], values[-window:]):
        if left > 0:
            returns.append(right / left - 1.0)
    if len(returns) < 2:
        return 0.0
    return _std(returns) * math.sqrt(252.0)


def score_factors(
    closes,
    volumes=None,
    short_window=SHORT_WINDOW,
    mid_window=MID_WINDOW,
    long_window=LONG_WINDOW,
):
    """Return factor diagnostics and a bounded composite score.

    The returned ``score`` is comparable across candidates.  A missing or
    insufficient history returns ``None`` and is intentionally not tradable.
    """
    prices = _clean(closes)
    vols = _clean(volumes or [])
    required = max(long_window + 1, mid_window + 1, 21)
    if len(prices) < required:
        return {"score": None, "trend": False, "reason": "insufficient_history"}

    momentum_short = _return(prices, short_window) or 0.0
    momentum_mid = _return(prices, mid_window) or 0.0
    momentum_long = _return(prices, long_window) or 0.0
    momentum = _clip(0.5 * momentum_short + 0.3 * momentum_mid + 0.2 * momentum_long)

    sma_mid = _sma(prices, mid_window)
    sma_long = _sma(prices, long_window)
    price = prices[-1]
    trend = bool(sma_mid and sma_long and price > sma_long and sma_mid > sma_long)
    trend_score = _clip(
        0.6 * (price / sma_mid - 1.0) * 8.0
        + 0.4 * (sma_mid / sma_long - 1.0) * 8.0
    )

    breadth_window = min(mid_window, len(prices) - 1)
    up_days = sum(
        1 for left, right in zip(prices[-breadth_window - 1:-1], prices[-breadth_window:])
        if right > left
    )
    breadth = (up_days / float(breadth_window)) * 2.0 - 1.0 if breadth_window else 0.0

    liquidity = 0.0
    if len(vols) >= 40:
        recent_volume = _mean(vols[-20:])
        base_volume = _mean(vols[-40:-20])
        if base_volume > 0:
            liquidity = _clip((recent_volume / base_volume - 1.0) * 2.0)

    volatility = _annualized_volatility(prices, 20) or 0.0
    volatility_penalty = _clip(volatility / 0.60, 0.0, 1.0)
    peak = max(prices[-long_window:])
    drawdown = price / peak - 1.0 if peak > 0 else 0.0
    drawdown_penalty = _clip(-drawdown / 0.25, 0.0, 1.0)

    score = (
        0.45 * momentum
        + 0.30 * trend_score
        + 0.15 * breadth
        + 0.10 * liquidity
        - 0.25 * volatility_penalty
        - 0.15 * drawdown_penalty
    )
    if not trend:
        score -= 0.25

    return {
        "score": score,
        "trend": trend,
        "momentum": momentum,
        "trend_score": trend_score,
        "breadth": breadth,
        "liquidity": liquidity,
        "volatility": volatility,
        "drawdown": drawdown,
        "price": price,
    }


def rank_candidates(
    history,
    max_positions=MAX_POSITIONS,
    min_score=MIN_SCORE,
    require_trend=REQUIRE_TREND,
):
    """Rank tradable candidates from ``{symbol: {close, volume}}`` history."""
    ranked = []
    for symbol, data in (history or {}).items():
        diagnostics = score_factors(data.get("close", []), data.get("volume", []))
        score = diagnostics.get("score")
        if score is None or (require_trend and not diagnostics.get("trend")) or score < min_score:
            continue
        item = dict(diagnostics)
        item["symbol"] = symbol
        ranked.append(item)
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[: max(0, int(max_positions))]


def _inverse_volatility_weights(items, gross_weight, max_position_weight):
    """Convert ranked candidates into capped inverse-volatility weights.

    Lower-volatility instruments receive more capital, but no single risk ETF
    can exceed ``max_position_weight``.  Any unallocated risk budget is left
    for the defensive leg instead of being forced into a volatile candidate.
    """
    if not items or gross_weight <= 0:
        return {}
    raw = {
        item["symbol"]: 1.0 / max(float(item.get("volatility") or 0.0), 0.08)
        for item in items
    }
    total = sum(raw.values())
    weights = {
        symbol: min(gross_weight * value / total, max_position_weight)
        for symbol, value in raw.items()
    }
    for _ in range(len(items) + 1):
        residual = gross_weight - sum(weights.values())
        uncapped = [
            symbol for symbol in raw
            if weights[symbol] < max_position_weight - 1e-12
        ]
        if residual <= 1e-10 or not uncapped:
            break
        denominator = sum(raw[symbol] for symbol in uncapped)
        for symbol in uncapped:
            addition = residual * raw[symbol] / denominator
            weights[symbol] = min(max_position_weight, weights[symbol] + addition)
    return weights


def build_target_weights(
    history,
    max_positions=MAX_POSITIONS,
    min_score=MIN_SCORE,
    require_trend=REQUIRE_TREND,
    volatility_target=VOLATILITY_TARGET,
    max_risk_weight=MAX_RISK_WEIGHT,
    max_position_weight=MAX_POSITION_WEIGHT,
    defensive_symbol=DEFENSIVE_SYMBOL,
):
    """Build long-only target weights for the production portfolio.

    The function is pure and therefore shared by unit tests and the external
    research harness.  It ranks only the risk universe, sizes selected names
    by inverse volatility, caps total risk exposure, and allocates the
    residual to the defensive ETF.  Returned weights never exceed 100%.
    """
    risk_history = {
        symbol: history.get(symbol, {}) for symbol in RISK_UNIVERSE
        if symbol in history
    }
    selected = rank_candidates(
        risk_history,
        max_positions=max_positions,
        min_score=min_score,
        require_trend=require_trend,
    )
    if selected:
        average_volatility = sum(item["volatility"] for item in selected) / float(len(selected))
        risk_weight = min(
            float(max_risk_weight),
            float(volatility_target) / max(average_volatility, 0.08),
        )
        weights = _inverse_volatility_weights(
            selected, risk_weight, float(max_position_weight)
        )
    else:
        weights = {}
    defensive_weight = max(0.0, 1.0 - sum(weights.values()))
    if defensive_symbol in history and history.get(defensive_symbol, {}).get("close"):
        weights[defensive_symbol] = defensive_weight
    return weights


def normalize_position_records(records, universe):
    """Normalize QMT ``POSITION`` objects into symbol/volume/entry data."""
    allowed = set(universe or ())
    normalized = {}
    for record in records or []:
        code = str(getattr(record, "m_strInstrumentID", "") or "").strip().upper()
        exchange = str(getattr(record, "m_strExchangeID", "") or "").strip().upper()
        if not code:
            continue
        symbol = code if "." in code else (code + "." + exchange if exchange else code)
        if allowed and symbol not in allowed:
            continue
        try:
            volume = int(float(getattr(record, "m_nVolume", 0) or 0))
        except (TypeError, ValueError):
            continue
        if volume <= 0:
            continue
        try:
            entry_price = float(getattr(record, "m_dOpenAvgPrice", 0.0) or 0.0)
        except (TypeError, ValueError):
            entry_price = 0.0
        normalized[symbol] = {
            "volume": volume,
            "entry_price": entry_price if entry_price > 0 else None,
        }
    return normalized


_ACTIVE_ORDER_STATUS = (49, 50, 51, 52, 55)


def has_pending_orders(records, universe):
    """Return True when an allowed symbol still has executable order volume."""
    allowed = set(universe or ())
    for record in records or []:
        code = str(getattr(record, "m_strInstrumentID", "") or "").strip().upper()
        exchange = str(getattr(record, "m_strExchangeID", "") or "").strip().upper()
        symbol = code if "." in code else (code + "." + exchange if exchange else code)
        if allowed and symbol not in allowed:
            continue
        try:
            status = int(getattr(record, "m_nOrderStatus"))
        except (AttributeError, TypeError, ValueError):
            status = None
        try:
            remaining = int(float(getattr(record, "m_nVolumeTotal")))
        except (AttributeError, TypeError, ValueError):
            remaining = None
        if remaining is not None and remaining <= 0:
            continue
        if status in _ACTIVE_ORDER_STATUS or (status is None and remaining is not None):
            return True
    return False


def can_route_orders(context, enabled, live_confirm_token, account_id=None):
    """Allow backtest orders; require token and account allow-list for live."""
    mode = str(getattr(context, "trade_mode", "")).lower()
    if mode == "backtest" or getattr(context, "do_back_test", 0) == 1:
        return bool(enabled)
    return bool(
        enabled
        and live_confirm_token == "I_UNDERSTAND_LIVE_TRADING"
        and account_id
        and str(account_id) in set(VALID_ACCOUNTS)
    )


def _is_backtest(C):
    """Recognize both modern and legacy QMT backtest context flags."""
    return str(getattr(C, "trade_mode", "")).lower() == "backtest" or getattr(C, "do_back_test", 0) == 1


def _history_map(C, field, count):
    """Read one daily field from QMT and normalize it by symbol."""
    try:
        raw = C.get_history_data(count, "1d", field, 0)
    except Exception as exc:
        print("[multifactor_live] %s history error: %s" % (field, exc))
        return {}
    if not hasattr(raw, "items"):
        return {}
    return {symbol: _clean(values) for symbol, values in raw.items()}


def _account_id(C):
    """Resolve QMT's account id variants without hard-coding a user account."""
    for candidate in (
        globals().get("account", ""),
        getattr(C, "account", ""),
        getattr(C, "accID", ""),
        getattr(C, "account_id", ""),
    ):
        if candidate:
            return str(candidate)
    return "BACKTEST" if _is_backtest(C) else ""


def _account_positions(C):
    """Read positions when QMT exposes its account query function.

    ``None`` means the query is unavailable/failed, while ``{}`` is a valid
    empty account snapshot.  Keeping that distinction avoids erasing local
    state on a transient API error.
    """
    if _is_backtest(C):
        return None
    account_id = _account_id(C)
    query_fn = globals().get("get_trade_detail_data")
    if not account_id or account_id == "BACKTEST" or not callable(query_fn):
        return None
    try:
        records = query_fn(account_id, "STOCK", "POSITION")
    except Exception as exc:
        print("[multifactor_live] position query error: %s" % exc)
        return None
    return normalize_position_records(records, getattr(g, "symbols", UNIVERSE))


def normalize_asset_records(records):
    """Read total equity and available cash from broker-specific ASSET rows."""
    rows = records if isinstance(records, (list, tuple)) else [records]
    for record in rows:
        if record is None:
            continue
        total = None
        available = None
        for name in ("m_dTotalAsset", "m_dBalance", "m_dNetAsset"):
            try:
                value = float(getattr(record, name))
            except (AttributeError, TypeError, ValueError):
                continue
            if value > 0:
                total = value
                break
        for name in ("m_dAvailable", "m_dAvailableCash"):
            try:
                value = float(getattr(record, name))
            except (AttributeError, TypeError, ValueError):
                continue
            if value >= 0:
                available = value
                break
        if total is not None:
            return {"total_asset": total, "available_cash": available}
    return None


def _account_asset(C):
    """Return a conservative capital snapshot or ``None`` on live failures."""
    if _is_backtest(C):
        for name in ("capital", "initial_capital", "init_capital"):
            try:
                value = float(getattr(C, name))
            except (AttributeError, TypeError, ValueError):
                continue
            if value > 0:
                return {"total_asset": value, "available_cash": value}
        return {"total_asset": MODEL_CAPITAL, "available_cash": MODEL_CAPITAL}
    account_id = _account_id(C)
    query_fn = globals().get("get_trade_detail_data")
    if not account_id or not callable(query_fn):
        return None
    try:
        records = query_fn(account_id, "STOCK", "ASSET")
    except Exception as exc:
        print("[multifactor_live] asset query error: %s" % exc)
        return None
    return normalize_asset_records(records)


def _has_pending_order(C, symbol):
    """Fail closed when live order status cannot be read reliably."""
    if _is_backtest(C):
        return False
    account_id = _account_id(C)
    query_fn = globals().get("get_trade_detail_data")
    if not account_id:
        return False
    if not callable(query_fn):
        print("[multifactor_live] order query unavailable; order skipped")
        return True
    try:
        try:
            records = query_fn(account_id, "STOCK", "ORDER", getattr(g, "strategy_name", "multifactor_live"))
        except TypeError:
            records = query_fn(account_id, "STOCK", "ORDER")
    except Exception as exc:
        print("[multifactor_live] order query error: %s" % exc)
        return True
    return has_pending_orders(records, {symbol})


def _sync_positions(C, history):
    """Rebuild live position, entry and peak state from a broker snapshot."""
    snapshot = _account_positions(C)
    if snapshot is None:
        return
    for symbol in list(g.positions):
        if symbol not in snapshot:
            del g.positions[symbol]
            g.entry_prices.pop(symbol, None)
            g.peak_prices.pop(symbol, None)
    for symbol, data in snapshot.items():
        g.positions[symbol] = data["volume"]
        current = history.get(symbol, {}).get("close", [])
        current_price = current[-1] if current else None
        g.entry_prices[symbol] = data["entry_price"] or current_price
        g.peak_prices[symbol] = max(
            g.peak_prices.get(symbol, current_price or 0.0), current_price or 0.0
        ) or None


def _target_volume(target_value, price):
    """Convert a target market value to a whole-lot share quantity."""
    if not price or price <= 0 or target_value <= 0:
        return 0
    return int(float(target_value) / price / ORDER_LOT) * ORDER_LOT


def _tradable_now(data):
    """Reject missing liquidity and approximate limit-up/limit-down closes."""
    closes = data.get("close", [])
    volumes = data.get("volume", [])
    if len(closes) < 2 or not volumes or volumes[-1] <= 0:
        return False
    change = closes[-1] / closes[-2] - 1.0 if closes[-2] > 0 else 0.0
    return abs(change) < 0.095


def _send_order(C, signal, symbol, price, volume):
    """Submit one bounded child order after every live/backtest safety gate.

    A zero ``passorder`` result means QMT accepted the request for processing;
    it is not treated as proof of a fill.  Reconciliation happens separately.
    """
    if volume <= 0:
        return False
    if getattr(g, "orders_this_bar", 0) >= MAX_ORDERS_PER_BAR:
        print("[multifactor_live] per-bar order cap reached; order skipped")
        return False
    order_value = float(price) * int(volume)
    if order_value > MAX_ORDER_VALUE + max(1.0, price * ORDER_LOT):
        print("[multifactor_live] order value exceeds cap; order skipped: %.2f" % order_value)
        return False
    route_enabled = BACKTEST_ORDERS if _is_backtest(C) else ENABLE_ORDERS
    account_id = _account_id(C)
    if not can_route_orders(C, route_enabled, LIVE_CONFIRM_TOKEN, account_id):
        print("[multifactor_live] %s %s volume=%s (order gate closed)" % (signal, symbol, volume))
        return False
    if not account_id:
        print("[multifactor_live] no account selected; order skipped")
        return False
    if _has_pending_order(C, symbol):
        print("[multifactor_live] pending order exists; order skipped: %s" % symbol)
        return False
    passorder_fn = globals().get("passorder")
    if not callable(passorder_fn):
        print("[multifactor_live] passorder unavailable; signal only: %s %s" % (signal, symbol))
        return False
    operation = 23 if signal == "BUY" else 24
    try:
        result = passorder(
            operation,
            1101,
            account_id,
            symbol,
            5,
            -1,
            volume,
            "multifactor_live",
            0,
            "multifactor_%s" % signal.lower(),
            C,
        )
    except Exception as exc:
        print("[multifactor_live] passorder error: %s" % exc)
        return False
    if isinstance(result, (int, float)) and result not in (0,):
        print("[multifactor_live] passorder rejected: %s" % result)
        return False
    g.orders_this_bar = getattr(g, "orders_this_bar", 0) + 1
    print("[multifactor_live] %s %s volume=%s result=%s" % (signal, symbol, volume, result))
    return True


def _send_order_sliced(C, signal, symbol, price, volume):
    """Split a large rebalance into bounded whole-lot child orders."""
    max_volume = int(MAX_ORDER_VALUE / price / ORDER_LOT) * ORDER_LOT
    if max_volume <= 0:
        return 0
    remaining = int(volume)
    submitted = 0
    while remaining > 0 and getattr(g, "orders_this_bar", 0) < MAX_ORDERS_PER_BAR:
        child = min(remaining, max_volume)
        if not _send_order(C, signal, symbol, price, child):
            break
        submitted += child
        remaining -= child
    return submitted


def init(C):
    """Initialize deterministic runtime state and register the full universe."""
    g.symbols = list(UNIVERSE)
    g.positions = {}
    g.entry_prices = {}
    g.peak_prices = {}
    g.last_rebalance_bar = None
    g.strategy_name = "multifactor_live"
    g.orders_this_bar = 0
    C.set_universe(g.symbols)


def handlebar(C):
    """Reconcile the account, compute weights, then sell before buying.

    Live state is never assumed from an order submission: it is refreshed from
    QMT positions on the next rebalance.  Backtests maintain a predicted local
    state because the chart engine does not expose live POSITION snapshots.
    """
    barpos = int(getattr(C, "barpos", 0))
    if g.last_rebalance_bar is not None and barpos - g.last_rebalance_bar < REBALANCE_EVERY_BARS:
        return
    g.last_rebalance_bar = barpos
    g.orders_this_bar = 0

    close_map = _history_map(C, "close", MIN_HISTORY + 20)
    volume_map = _history_map(C, "volume", MIN_HISTORY + 20)
    history = {
        symbol: {"close": close_map.get(symbol, []), "volume": volume_map.get(symbol, [])}
        for symbol in g.symbols
    }
    _sync_positions(C, history)
    asset = _account_asset(C)
    if asset is None or asset.get("total_asset", 0.0) <= 0:
        print("[multifactor_live] asset snapshot unavailable; rebalance skipped")
        return
    capital = float(asset["total_asset"])
    target_weights = build_target_weights(history)

    # Hard exits override the ranking model.  Capital released by an exit is
    # redirected to the defensive ETF rather than immediately recycled into
    # another risk asset on the same bar.
    forced_exit_weight = 0.0
    for symbol in list(g.positions):
        data = history.get(symbol, {})
        closes = data.get("close", [])
        if not closes:
            continue
        price = closes[-1]
        entry = g.entry_prices.get(symbol, price) or price
        peak = max(g.peak_prices.get(symbol, price) or price, price)
        g.peak_prices[symbol] = peak
        stop = symbol in RISK_UNIVERSE and price <= entry * (1.0 - STOP_LOSS)
        take = symbol in RISK_UNIVERSE and price >= entry * (1.0 + TAKE_PROFIT)
        trailing = symbol in RISK_UNIVERSE and price <= peak * (1.0 - TRAILING_STOP)
        if stop or take or trailing:
            forced_exit_weight += target_weights.pop(symbol, 0.0)
    if forced_exit_weight > 0 and history.get(DEFENSIVE_SYMBOL, {}).get("close"):
        target_weights[DEFENSIVE_SYMBOL] = min(
            1.0, target_weights.get(DEFENSIVE_SYMBOL, 0.0) + forced_exit_weight
        )

    target_volumes = {}
    for symbol in g.symbols:
        closes = history.get(symbol, {}).get("close", [])
        price = closes[-1] if closes else None
        target_value = capital * target_weights.get(symbol, 0.0)
        target_volumes[symbol] = _target_volume(target_value, price)

    # Sell first so the broker can release cash before any buy is submitted.
    for symbol in g.symbols:
        current = int(g.positions.get(symbol, 0))
        target = int(target_volumes.get(symbol, 0))
        difference = current - target
        closes = history.get(symbol, {}).get("close", [])
        if difference <= 0 or not closes:
            continue
        price = closes[-1]
        if difference * price < MIN_REBALANCE_VALUE:
            continue
        submitted = _send_order_sliced(C, "SELL", symbol, price, difference)
        if submitted and _is_backtest(C):
            remaining = max(0, current - submitted)
            if remaining:
                g.positions[symbol] = remaining
            else:
                g.positions.pop(symbol, None)
                g.entry_prices.pop(symbol, None)
                g.peak_prices.pop(symbol, None)

    available_cash = asset.get("available_cash")
    cash_budget = float(available_cash) if available_cash is not None else capital
    for symbol in sorted(g.symbols, key=lambda item: target_weights.get(item, 0.0), reverse=True):
        current = int(g.positions.get(symbol, 0))
        target = int(target_volumes.get(symbol, 0))
        difference = target - current
        data = history.get(symbol, {})
        closes = data.get("close", [])
        if difference <= 0 or not closes or not _tradable_now(data):
            continue
        price = closes[-1]
        difference = min(difference, int(cash_budget / price / ORDER_LOT) * ORDER_LOT)
        if difference <= 0 or difference * price < MIN_REBALANCE_VALUE:
            continue
        submitted = _send_order_sliced(C, "BUY", symbol, price, difference)
        if submitted:
            cash_budget = max(0.0, cash_budget - submitted * price)
        if submitted and _is_backtest(C):
            g.positions[symbol] = current + submitted
            if symbol not in g.entry_prices:
                g.entry_prices[symbol] = price
                g.peak_prices[symbol] = price
