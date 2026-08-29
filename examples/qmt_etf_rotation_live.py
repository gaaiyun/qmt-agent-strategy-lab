# coding: gbk
"""ETF momentum rotation strategy for QMT.

The default universe is a liquid ETF basket with a bond ETF defensive asset.
Missing symbols are skipped, which keeps the model usable with a partial local
cache.  Live routing requires both ``ENABLE_ORDERS`` and an exact confirmation
token; QMT backtest routing is separately enabled for chart statistics.
"""

import math


ENABLE_ORDERS = False
BACKTEST_ORDERS = True
LIVE_CONFIRM_TOKEN = ""
VALID_ACCOUNTS = ()

# One representative per broad/style/industry sleeve.  The model ranks the
# available symbols and can fall back to the bond ETF when history is missing
# or the trend gate is closed.  Confirm each code is tradable in the local QMT
# data service before promoting a candidate.
RISK_UNIVERSE = (
    "510300.SH", "510500.SH", "159915.SZ", "512100.SH", "588000.SH",
    "512890.SH", "512480.SH", "512930.SH", "515230.SH", "512170.SH",
    "159928.SZ", "512800.SH", "512880.SH", "512400.SH", "515220.SH",
    "515790.SH", "512660.SH", "513100.SH", "513500.SH", "513050.SH",
    "518880.SH",
)
DEFENSIVE_SYMBOL = "511010.SH"
UNIVERSE = RISK_UNIVERSE + (DEFENSIVE_SYMBOL,)
MODEL_CAPITAL = 100000.0
REBALANCE_EVERY_BARS = 10
SHORT_WINDOW = 5
MID_WINDOW = 20
LONG_WINDOW = 120
MIN_SCORE = 0.05
MIN_HISTORY = max(LONG_WINDOW + 1, 101)
ORDER_LOT = 100
VOLATILITY_TARGET = 0.16
MAX_RISK_WEIGHT = 0.75
MIN_RISK_WEIGHT = 0.25
SWITCH_BUFFER = 0.0
MIN_REBALANCE_VALUE = 2000.0
MAX_ORDER_VALUE = 50000.0
MAX_ORDERS_PER_BAR = 8
STOP_LOSS = 0.10
TRAILING_STOP = 0.12


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


def _mean(values):
    """Return the arithmetic mean, using zero for an empty sequence."""
    return sum(values) / float(len(values)) if values else 0.0


def _std(values):
    """Return population standard deviation for deterministic scores."""
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / float(len(values)))


def _return(values, window):
    """Return the trailing simple return over ``window`` bars."""
    if len(values) <= window or values[-window - 1] <= 0:
        return None
    return values[-1] / values[-window - 1] - 1.0


def _annualized_volatility(values, window=20):
    """Estimate annualized close-to-close volatility with 252 trading days."""
    if len(values) < window + 1:
        return None
    returns = [right / left - 1.0 for left, right in zip(values[-window - 1:-1], values[-window:]) if left > 0]
    return _std(returns) * math.sqrt(252.0) if len(returns) >= 2 else 0.0


def _clip(value, lower=-1.0, upper=1.0):
    """Clamp a numeric value to a closed interval."""
    return max(lower, min(upper, float(value)))


def rotation_score(
    closes,
    short_window=SHORT_WINDOW,
    mid_window=MID_WINDOW,
    long_window=LONG_WINDOW,
):
    """Calculate risk-adjusted multi-horizon momentum for one ETF."""
    prices = _clean(closes)
    if len(prices) < max(long_window + 1, 101):
        return None
    price = prices[-1]
    short_return = _return(prices, short_window) or 0.0
    mid_return = _return(prices, mid_window) or 0.0
    long_return = _return(prices, long_window) or 0.0
    momentum = 0.5 * short_return + 0.3 * mid_return + 0.2 * long_return
    sma50 = _mean(prices[-50:])
    sma100 = _mean(prices[-100:])
    trend = price > sma100 and sma50 > sma100
    volatility = _annualized_volatility(prices, 20) or 0.0
    peak = max(prices[-long_window:])
    drawdown = price / peak - 1.0 if peak > 0 else 0.0
    score = momentum - 0.35 * _clip(volatility / 0.60, 0.0, 1.0) - 0.10 * _clip(-drawdown / 0.25, 0.0, 1.0)
    if trend:
        score += 0.10
    else:
        score -= 0.20
    return {
        "score": score,
        "trend": trend,
        "momentum": momentum,
        "volatility": volatility,
        "drawdown": drawdown,
        "price": price,
    }


def rank_candidates(history):
    """Score available symbols and sort them from strongest to weakest."""
    ranked = []
    for symbol, closes in (history or {}).items():
        diagnostics = rotation_score(closes)
        if diagnostics is None:
            continue
        item = dict(diagnostics)
        item["symbol"] = symbol
        ranked.append(item)
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked


def select_target(ranked, defensive_symbol=DEFENSIVE_SYMBOL, min_score=MIN_SCORE):
    """Select the strongest risk ETF, otherwise the defensive ETF if available."""
    defensive = None
    for item in ranked or []:
        if item.get("symbol") == defensive_symbol:
            defensive = item
        if (
            item.get("symbol") != defensive_symbol
            and item.get("trend")
            and item.get("score", -1.0) >= min_score
        ):
            return item["symbol"]
    return defensive_symbol if defensive is not None else None


def build_target_weights(
    history,
    current_risk_symbol=None,
    min_score=MIN_SCORE,
    volatility_target=VOLATILITY_TARGET,
    max_risk_weight=MAX_RISK_WEIGHT,
    min_risk_weight=MIN_RISK_WEIGHT,
    switch_buffer=SWITCH_BUFFER,
    defensive_symbol=DEFENSIVE_SYMBOL,
):
    """Return a volatility-budgeted risk/defensive allocation.

    At most one risk ETF is held.  Its weight is reduced when realized
    volatility rises; the residual is allocated to the defensive ETF.  A
    switch buffer can keep the current risk ETF unless a challenger has a
    materially better score.
    """
    ranked = []
    for symbol in RISK_UNIVERSE:
        diagnostics = rotation_score(history.get(symbol, []))
        if diagnostics and diagnostics["trend"] and diagnostics["score"] >= min_score:
            item = dict(diagnostics)
            item["symbol"] = symbol
            ranked.append(item)
    ranked.sort(key=lambda item: item["score"], reverse=True)
    chosen = ranked[0] if ranked else None
    current = next(
        (item for item in ranked if item["symbol"] == current_risk_symbol),
        None,
    )
    if current and chosen and chosen["score"] < current["score"] + switch_buffer:
        chosen = current

    weights = {}
    if chosen:
        risk_weight = min(
            float(max_risk_weight),
            float(volatility_target) / max(chosen["volatility"], 0.08),
        )
        if risk_weight >= min_risk_weight:
            weights[chosen["symbol"]] = risk_weight
    defensive_weight = max(0.0, 1.0 - sum(weights.values()))
    if defensive_symbol in history and history.get(defensive_symbol):
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


def _history_map(C, count):
    """Read daily closes from QMT and normalize them by symbol."""
    try:
        raw = C.get_history_data(count, "1d", "close", 0)
    except Exception as exc:
        print("[etf_rotation_live] history error: %s" % exc)
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
    """Read positions when QMT exposes its account query function."""
    if _is_backtest(C):
        return None
    account_id = _account_id(C)
    query_fn = globals().get("get_trade_detail_data")
    if not account_id or account_id == "BACKTEST" or not callable(query_fn):
        return None
    try:
        records = query_fn(account_id, "STOCK", "POSITION")
    except Exception as exc:
        print("[etf_rotation_live] position query error: %s" % exc)
        return None
    return normalize_position_records(records, getattr(g, "symbols", UNIVERSE))


def normalize_asset_records(records):
    """Read total equity and available cash from QMT ASSET rows."""
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
    """Return capital for sizing; fail closed when a live query is missing."""
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
        print("[etf_rotation_live] asset query error: %s" % exc)
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
        print("[etf_rotation_live] order query unavailable; order skipped")
        return True
    try:
        try:
            records = query_fn(account_id, "STOCK", "ORDER", getattr(g, "strategy_name", "etf_rotation_live"))
        except TypeError:
            records = query_fn(account_id, "STOCK", "ORDER")
    except Exception as exc:
        print("[etf_rotation_live] order query error: %s" % exc)
        return True
    return has_pending_orders(records, {symbol})


def _sync_positions(C, history):
    """Reconcile all risk and defensive holdings from the broker snapshot."""
    snapshot = _account_positions(C)
    if snapshot is None:
        return
    for symbol in list(g.positions):
        if symbol not in snapshot:
            g.positions.pop(symbol, None)
            g.entry_prices.pop(symbol, None)
            g.peak_prices.pop(symbol, None)
    for symbol, data in snapshot.items():
        g.positions[symbol] = data["volume"]
        current = history.get(symbol, [])
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


def _tradable_now(closes):
    """Reject missing prices and approximate limit-up/limit-down closes."""
    if len(closes) < 2:
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
        print("[etf_rotation_live] per-bar order cap reached; order skipped")
        return False
    order_value = float(price) * int(volume)
    if order_value > MAX_ORDER_VALUE + max(1.0, price * ORDER_LOT):
        print("[etf_rotation_live] order value exceeds cap; order skipped: %.2f" % order_value)
        return False
    route_enabled = BACKTEST_ORDERS if _is_backtest(C) else ENABLE_ORDERS
    account_id = _account_id(C)
    if not can_route_orders(C, route_enabled, LIVE_CONFIRM_TOKEN, account_id):
        print("[etf_rotation_live] %s %s volume=%s (order gate closed)" % (signal, symbol, volume))
        return False
    if not account_id:
        print("[etf_rotation_live] no account selected; order skipped")
        return False
    if _has_pending_order(C, symbol):
        print("[etf_rotation_live] pending order exists; order skipped: %s" % symbol)
        return False
    passorder_fn = globals().get("passorder")
    if not callable(passorder_fn):
        print("[etf_rotation_live] passorder unavailable; signal only: %s %s" % (signal, symbol))
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
            "etf_rotation_live",
            0,
            "etf_rotation_%s" % signal.lower(),
            C,
        )
    except Exception as exc:
        print("[etf_rotation_live] passorder error: %s" % exc)
        return False
    if isinstance(result, (int, float)) and result not in (0,):
        print("[etf_rotation_live] passorder rejected: %s" % result)
        return False
    g.orders_this_bar = getattr(g, "orders_this_bar", 0) + 1
    print("[etf_rotation_live] %s %s volume=%s result=%s" % (signal, symbol, volume, result))
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
    g.risk_symbol = None
    g.strategy_name = "etf_rotation_live"
    g.orders_this_bar = 0
    C.set_universe(g.symbols)


def handlebar(C):
    """Reconcile positions and rebalance a risk/defensive two-leg portfolio."""
    barpos = int(getattr(C, "barpos", 0))
    if g.last_rebalance_bar is not None and barpos - g.last_rebalance_bar < REBALANCE_EVERY_BARS:
        return
    g.last_rebalance_bar = barpos
    g.orders_this_bar = 0

    close_map = _history_map(C, MIN_HISTORY + 20)
    history = {symbol: close_map.get(symbol, []) for symbol in g.symbols}
    _sync_positions(C, history)
    asset = _account_asset(C)
    if asset is None or asset.get("total_asset", 0.0) <= 0:
        print("[etf_rotation_live] asset snapshot unavailable; rebalance skipped")
        return
    capital = float(asset["total_asset"])

    current_risk = next((symbol for symbol in RISK_UNIVERSE if g.positions.get(symbol, 0) > 0), None)
    target_weights = build_target_weights(history, current_risk_symbol=current_risk)

    # Emergency risk exits override the model and move exposure to defense.
    for symbol in RISK_UNIVERSE:
        if g.positions.get(symbol, 0) <= 0 or not history.get(symbol):
            continue
        price = history[symbol][-1]
        entry = g.entry_prices.get(symbol, price) or price
        peak = max(g.peak_prices.get(symbol, price) or price, price)
        g.peak_prices[symbol] = peak
        stop = price <= entry * (1.0 - STOP_LOSS)
        trailing = price <= peak * (1.0 - TRAILING_STOP)
        if stop or trailing:
            released = target_weights.pop(symbol, 0.0)
            target_weights[DEFENSIVE_SYMBOL] = min(
                1.0, target_weights.get(DEFENSIVE_SYMBOL, 0.0) + released
            )

    target_volumes = {}
    for symbol in g.symbols:
        closes = history.get(symbol, [])
        price = closes[-1] if closes else None
        target_volumes[symbol] = _target_volume(
            capital * target_weights.get(symbol, 0.0), price
        )

    for symbol in g.symbols:
        current = int(g.positions.get(symbol, 0))
        target = int(target_volumes.get(symbol, 0))
        difference = current - target
        closes = history.get(symbol, [])
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
        closes = history.get(symbol, [])
        if difference <= 0 or not _tradable_now(closes):
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
