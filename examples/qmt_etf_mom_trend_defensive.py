# coding: utf-8
"""ETF_MOM_TREND_DEFENSIVE：月频动量、趋势与防守腿策略。

本文件是给 QMT ``init(C)``/``handlebar(C)`` 使用的独立候选，不覆盖旧策略。
信号只使用已经完成的历史收盘价，并在默认 T+1 规则下把最近一根历史值
留作当前 bar，计算窗口跳过最近 21 个交易日，避免把尚未完成的 bar 当成
可用信息。默认 ``ENABLE_ORDERS=False``；本文件本身不连接账户，也不会在
默认配置下调用 ``passorder``。

研究规则：6/12 个月动量（126/252 个交易日）按近 21 日跳过后计算，动量
再除以 60 日年化波动率形成风险调整分数；只保留绝对动量为正且处于长期
上升趋势的风险 ETF，取 Top 3，逆波动配置，单 ETF 不超过 40%，组合目标
年化波动率 12%。每次调仓的单边换手不超过 25%，剩余资金进入货币/短债
防守 ETF。股票 ETF 主规则为 T+1；若将来有 point-in-time 产品 registry，
也应只对明确允许 T+0 的品种单独放行，不能在这里默认放开。
"""

import datetime
import math


# --------------------------- 安全与模型参数 ---------------------------

# 实盘委托总开关必须保持字面量 False，供静态 validator 和人工审阅识别。
ENABLE_ORDERS = False
# 回测与实盘路由分开；即使将来开启实盘，也不会复用回测账户。
BACKTEST_ORDERS = True
LIVE_CONFIRM_TOKEN = ""
VALID_ACCOUNTS = ()

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
ORDER_LOT = 100
MAX_ORDERS_PER_BAR = 8
MAX_ORDER_VALUE = 50000.0
MIN_REBALANCE_VALUE = 2000.0

# 126/252 约对应六个月/十二个月；SKIP_BARS=21 是跳过最近一个月。
MOMENTUM_WINDOWS = (126, 252)
SKIP_BARS = 21
VOLATILITY_WINDOW = 60
TREND_FAST_WINDOW = 50
TREND_SLOW_WINDOW = 200
TOP_N = 3
VOLATILITY_TARGET = 0.12
MAX_POSITION_WEIGHT = 0.40
MAX_RISK_WEIGHT = 1.00
MAX_ONE_WAY_TURNOVER = 0.25

# 默认按股票 ETF 的 T+1 处理：本 bar 不使用本 bar 收盘价生成同 bar 委托。
T_PLUS_ONE = True
REBALANCE_FREQUENCY = "month_start_after_month_end_signal"


class RuntimeState(object):
    """QMT 回调之间保存的最小状态；不保存账户凭据。"""


g = RuntimeState()


def _clean(values):
    """清洗价格序列，只保留有限且为正的数字，不用零值补齐缺失数据。"""
    result = []
    for value in values or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            result.append(number)
    return result


def _mean(values):
    """计算均值；空序列返回 0，调用方必须在使用前检查样本长度。"""
    return sum(values) / float(len(values)) if values else 0.0


def _std(values):
    """计算总体标准差，保持纯 Python、无 pandas 依赖。"""
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / float(len(values)))


def _annualized_volatility(closes, window=VOLATILITY_WINDOW):
    """用收盘到收盘收益估计年化波动率；样本不足时返回 None。"""
    prices = _clean(closes)
    if len(prices) < window + 1:
        return None
    returns = [
        right / left - 1.0
        for left, right in zip(prices[-window - 1:-1], prices[-window:])
        if left > 0
    ]
    if len(returns) < 2:
        return None
    return _std(returns) * math.sqrt(252.0)


def _month_key(timestamp):
    """把 QMT 的秒/毫秒时间戳转换成稳定的年月键。"""
    try:
        stamp = float(timestamp)
    except (TypeError, ValueError):
        return None
    if stamp > 100000000000.0:
        stamp /= 1000.0
    try:
        return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m")
    except (OverflowError, OSError, ValueError):
        return None


def _bar_month(C, barpos):
    """读取指定 bar 的年月；QMT 时间戳不可读时返回 None 并保持观望。"""
    try:
        return _month_key(C.get_bar_timetag(barpos))
    except Exception:
        return None


def _is_new_month(C):
    """在每个自然月第一根 bar 触发一次，等价于上一月末收盘信号次日执行。"""
    current = _bar_month(C, int(getattr(C, "barpos", 0)))
    if current is None:
        return False, None
    if getattr(g, "last_rebalance_month", None) == current:
        return False, current
    # 第一次回调也允许触发，但仍要经过历史长度检查；之后只在月份变化时触发。
    return True, current


def _lookup_symbol(raw, symbol):
    """兼容 QMT 返回带点、去点以及单一键值三种历史映射形态。"""
    if not hasattr(raw, "get"):
        return raw if len(UNIVERSE) == 1 else None
    values = raw.get(symbol)
    if values is None:
        values = raw.get(symbol.replace(".", ""))
    if values is None and len(raw) == 1:
        values = list(raw.values())[0]
    return values


def history_values(C, symbol, count):
    """使用 QMT 原生 get_history_data，不依赖 pandas DataFrame 包装。"""
    try:
        raw = C.get_history_data(count, "1d", "close", 0)
    except Exception as exc:
        print("[etf_mom_trend_defensive] history error: %s" % exc)
        return []
    return _clean(_lookup_symbol(raw, symbol))


def signal_history(values, t_plus_one=T_PLUS_ONE):
    """生成信号可用的历史收盘序列。

    QMT 不同版本对 ``get_history_data(..., 0)`` 是否含当前 bar 的表现可能
    不同。默认丢弃末值是保守的 T+1 处理：即使末值已完成，也只会多等一根
    bar，不会把当前 bar 的收盘价偷看后在同一根 bar 成交。
    """
    prices = _clean(values)
    if t_plus_one and len(prices) > 1:
        return prices[:-1]
    return prices


def momentum_diagnostics(
    closes,
    windows=MOMENTUM_WINDOWS,
    skip_bars=SKIP_BARS,
    volatility_window=VOLATILITY_WINDOW,
):
    """计算跳过最近 21 日的六/十二个月风险调整动量与绝对动量。"""
    prices = _clean(closes)
    required = max(max(windows) + skip_bars + 1, TREND_SLOW_WINDOW + skip_bars + 1, volatility_window + 1)
    if len(prices) < required:
        return None
    anchor = len(prices) - skip_bars - 1
    if anchor <= max(windows) or anchor < volatility_window:
        return None
    anchor_price = prices[anchor]
    momentum_returns = []
    for window in windows:
        base = prices[anchor - window]
        if base <= 0:
            return None
        momentum_returns.append(anchor_price / base - 1.0)
    recent_vol = _annualized_volatility(prices[: anchor + 1], volatility_window)
    if recent_vol is None or recent_vol <= 0:
        # 零波动不能用无限高分奖励；恒定价格也没有可交易的动量。
        return None
    six_month, twelve_month = momentum_returns[0], momentum_returns[1]
    risk_adjusted_score = 0.5 * six_month / recent_vol + 0.5 * twelve_month / recent_vol
    slow_start = anchor - TREND_SLOW_WINDOW + 1
    fast_start = anchor - TREND_FAST_WINDOW + 1
    slow_average = _mean(prices[slow_start: anchor + 1])
    fast_average = _mean(prices[fast_start: anchor + 1])
    trend = anchor_price > slow_average and fast_average > slow_average
    absolute_momentum = 0.5 * six_month + 0.5 * twelve_month
    return {
        "score": risk_adjusted_score,
        "momentum_6m": six_month,
        "momentum_12m": twelve_month,
        "absolute_momentum": absolute_momentum,
        "volatility": recent_vol,
        "trend": trend,
        "anchor_price": anchor_price,
        "anchor_index": anchor,
    }


def rank_candidates(history, top_n=TOP_N):
    """按风险调整动量排序，只返回通过绝对动量和趋势门的风险 ETF。"""
    ranked = []
    for symbol in RISK_UNIVERSE:
        diagnostics = momentum_diagnostics(history.get(symbol, []))
        if diagnostics is None:
            continue
        if diagnostics["absolute_momentum"] <= 0 or not diagnostics["trend"]:
            continue
        item = dict(diagnostics)
        item["symbol"] = symbol
        ranked.append(item)
    ranked.sort(key=lambda item: (item["score"], item["symbol"]), reverse=True)
    return ranked[: max(0, int(top_n))]


def _inverse_volatility_weights(items, gross_weight, cap=MAX_POSITION_WEIGHT):
    """在总风险预算内逆波动分配，并把超出 40% 的余量再分配给未封顶标的。"""
    if not items or gross_weight <= 0:
        return {}
    raw = {item["symbol"]: 1.0 / max(float(item["volatility"]), 0.01) for item in items}
    weights = {symbol: 0.0 for symbol in raw}
    remaining = float(gross_weight)
    open_symbols = set(raw)
    for _ in range(len(raw) + 1):
        if not open_symbols or remaining <= 1e-12:
            break
        denominator = sum(raw[symbol] for symbol in open_symbols)
        capped = set()
        for symbol in open_symbols:
            proposed = remaining * raw[symbol] / denominator
            room = cap - weights[symbol]
            addition = min(room, proposed)
            weights[symbol] += addition
            if addition + 1e-12 < proposed:
                capped.add(symbol)
        remaining = gross_weight - sum(weights.values())
        open_symbols -= capped
        if not capped:
            break
    return {symbol: weight for symbol, weight in weights.items() if weight > 1e-12}


def build_target_weights(
    history,
    top_n=TOP_N,
    volatility_target=VOLATILITY_TARGET,
    max_position_weight=MAX_POSITION_WEIGHT,
    defensive_symbol=DEFENSIVE_SYMBOL,
):
    """生成 Top3 风险 ETF + 防守腿的目标权重。

    所有风险 ETF 的绝对动量必须大于零；若没有合格标的，直接返回 100%
    防守腿。目标组合波动率用入选 ETF 波动率的逆波动加权平均作保守估计，
    只把风险总权重降下来，不通过杠杆放大仓位。
    """
    selected = rank_candidates(history, top_n=top_n)
    weights = {}
    if selected:
        inverse = {item["symbol"]: 1.0 / max(item["volatility"], 0.01) for item in selected}
        inverse_total = sum(inverse.values())
        estimated_vol = sum(item["volatility"] * inverse[item["symbol"]] for item in selected) / inverse_total
        gross_weight = min(MAX_RISK_WEIGHT, float(volatility_target) / max(estimated_vol, 0.01))
        weights = _inverse_volatility_weights(selected, gross_weight, cap=float(max_position_weight))
    # 防守腿吸收未使用的现金和风险预算，确保目标权重总和不超过 1。
    defensive_weight = max(0.0, 1.0 - sum(weights.values()))
    weights[defensive_symbol] = defensive_weight
    return weights


def one_way_turnover(current_weights, target_weights):
    """计算单边换手率：只计买入增量，等价于总买卖换手的一半。"""
    symbols = set((current_weights or {})) | set((target_weights or {}))
    return sum(
        max(0.0, float((target_weights or {}).get(symbol, 0.0)) - float((current_weights or {}).get(symbol, 0.0)))
        for symbol in symbols
    )


def apply_turnover_gate(current_weights, target_weights, max_one_way=MAX_ONE_WAY_TURNOVER):
    """把一次过大的调仓线性限幅到 25%，保留总权重为 1。"""
    current = dict(current_weights or {})
    target = dict(target_weights or {})
    if not current:
        current = {DEFENSIVE_SYMBOL: 1.0}
    turnover = one_way_turnover(current, target)
    if turnover <= float(max_one_way) + 1e-12:
        return target
    scale = float(max_one_way) / turnover
    symbols = set(current) | set(target)
    limited = {
        symbol: float(current.get(symbol, 0.0))
        + scale * (float(target.get(symbol, 0.0)) - float(current.get(symbol, 0.0)))
        for symbol in symbols
    }
    # 数值误差只做最后归一化，不把负权重或杠杆藏起来。
    limited = {symbol: max(0.0, value) for symbol, value in limited.items() if value > 1e-12}
    total = sum(limited.values())
    if total <= 0:
        return {DEFENSIVE_SYMBOL: 1.0}
    return {symbol: value / total for symbol, value in limited.items()}


def _is_backtest(C):
    """兼容新版 trade_mode 和旧版 do_back_test 标志。"""
    return str(getattr(C, "trade_mode", "")).lower() == "backtest" or getattr(C, "do_back_test", 0) == 1


def can_route_orders(C, enabled, live_confirm_token, account_id=None):
    """回测只看独立回测开关；实盘还必须同时满足 token 与账户白名单。"""
    if _is_backtest(C):
        return bool(enabled)
    return bool(
        enabled
        and live_confirm_token == "I_UNDERSTAND_LIVE_TRADING"
        and account_id
        and str(account_id) in set(VALID_ACCOUNTS)
    )


def _account_id(C):
    """读取 QMT 账户 ID 变体；不在日志中打印完整 ID。"""
    for candidate in (globals().get("account", ""), getattr(C, "account", ""), getattr(C, "accID", ""), getattr(C, "account_id", "")):
        if candidate:
            return str(candidate)
    return "BACKTEST" if _is_backtest(C) else ""


def _send_order(C, signal, symbol, price, volume):
    """统一订单门禁；缺少账户、订单查询或 passorder 时全部 fail-closed。"""
    if volume <= 0 or getattr(g, "orders_this_bar", 0) >= MAX_ORDERS_PER_BAR:
        return False
    order_value = float(price) * int(volume)
    if order_value > MAX_ORDER_VALUE + max(1.0, float(price) * ORDER_LOT):
        print("[%s] order value exceeds cap; skipped" % getattr(g, "strategy_name", "etf_mom_trend_defensive"))
        return False
    route_enabled = BACKTEST_ORDERS if _is_backtest(C) else ENABLE_ORDERS
    account_id = _account_id(C)
    if not can_route_orders(C, route_enabled and getattr(g, "enable_orders", False), LIVE_CONFIRM_TOKEN, account_id):
        print("[%s] %s %s volume=%s (order gate closed)" % (getattr(g, "strategy_name", "etf_mom_trend_defensive"), signal, symbol, volume))
        return False
    if not account_id or not callable(globals().get("passorder")):
        print("[%s] account/passorder unavailable; skipped" % getattr(g, "strategy_name", "etf_mom_trend_defensive"))
        return False
    operation = 23 if signal == "BUY" else 24
    try:
        result = passorder(
            operation, 1101, account_id, symbol, 5, -1, int(volume),
            getattr(g, "strategy_name", "etf_mom_trend_defensive"),
            0,  # quickTrade=0：允许历史回测逐 bar 处理，不只在最新 bar 触发。
            "%s_%s" % (getattr(g, "strategy_name", "etf_mom_trend_defensive"), signal.lower()), C,
        )
    except Exception as exc:
        print("[%s] passorder error: %s" % (getattr(g, "strategy_name", "etf_mom_trend_defensive"), exc))
        return False
    if isinstance(result, (int, float)) and result not in (0,):
        return False
    g.orders_this_bar = getattr(g, "orders_this_bar", 0) + 1
    return True


def _target_volume(target_value, price):
    """按 ETF 最小交易单位向下取整，缺价时返回零。"""
    if target_value <= 0 or price is None or float(price) <= 0:
        return 0
    return int(float(target_value) / float(price) / ORDER_LOT) * ORDER_LOT


def init(C):
    """初始化 QMT 宇宙、策略状态和默认关闭的订单开关。"""
    g.symbols = list(UNIVERSE)
    g.enable_orders = ENABLE_ORDERS
    g.strategy_name = "etf_mom_trend_defensive"
    g.last_rebalance_month = None
    g.orders_this_bar = 0
    # 未从券商确认持仓前，换手门禁以防守腿作为保守起点，不假设风险仓位存在。
    g.current_weights = {DEFENSIVE_SYMBOL: 1.0}
    g.last_target_weights = {DEFENSIVE_SYMBOL: 1.0}
    C.set_universe(g.symbols)


def handlebar(C):
    """月初读取上一月末信号并计划下一交易日执行；缺数据时保持防守/观望。"""
    triggered, month = _is_new_month(C)
    if not triggered:
        return
    g.last_rebalance_month = month

    history = {}
    required = max(MOMENTUM_WINDOWS) + SKIP_BARS + TREND_SLOW_WINDOW
    for symbol in g.symbols:
        history[symbol] = signal_history(history_values(C, symbol, required + 2), T_PLUS_ONE)
    if not history.get(DEFENSIVE_SYMBOL) or any(len(history.get(symbol, [])) < required for symbol in RISK_UNIVERSE):
        print("[%s] history incomplete; rebalance skipped" % g.strategy_name)
        return

    target = build_target_weights(history)
    limited = apply_turnover_gate(g.current_weights, target)
    g.last_target_weights = limited
    ranked = rank_candidates(history)
    print(
        "[%s] month=%s candidates=%s one_way_turnover=%.4f T+1=%s orders=%s"
        % (g.strategy_name, month, ",".join(item["symbol"] for item in ranked),
           one_way_turnover(g.current_weights, limited), T_PLUS_ONE, g.enable_orders)
    )

    # 默认只输出研究信号，不更新为“已成交”的持仓，也不调用账户/订单接口。
    if not g.enable_orders:
        print("[%s] signal-only mode; no order submitted" % g.strategy_name)
        return

    prices = {}
    for symbol in g.symbols:
        values = history.get(symbol, [])
        prices[symbol] = values[-1] if values else None
    capital = MODEL_CAPITAL
    current = dict(getattr(g, "positions", {}))
    target_volumes = {
        symbol: _target_volume(capital * limited.get(symbol, 0.0), prices.get(symbol))
        for symbol in g.symbols
    }
    g.orders_this_bar = 0
    # 卖出先行，释放现金；订单返回值只代表请求进入 QMT，不代表成交。
    for symbol in g.symbols:
        difference = int(current.get(symbol, 0)) - int(target_volumes.get(symbol, 0))
        if difference <= 0 or prices.get(symbol) is None:
            continue
        if _send_order(C, "SELL", symbol, prices[symbol], difference):
            current[symbol] = max(0, int(current.get(symbol, 0)) - difference)
    for symbol in sorted(g.symbols, key=lambda item: limited.get(item, 0.0), reverse=True):
        difference = int(target_volumes.get(symbol, 0)) - int(current.get(symbol, 0))
        if difference <= 0 or prices.get(symbol) is None:
            continue
        if _send_order(C, "BUY", symbol, prices[symbol], difference):
            current[symbol] = int(current.get(symbol, 0)) + difference
    g.positions = current


__all__ = [
    "BACKTEST_ORDERS", "DEFENSIVE_SYMBOL", "ENABLE_ORDERS", "MAX_ONE_WAY_TURNOVER",
    "MAX_POSITION_WEIGHT", "MOMENTUM_WINDOWS", "RISK_UNIVERSE", "SKIP_BARS",
    "T_PLUS_ONE", "TOP_N", "UNIVERSE", "VOLATILITY_TARGET", "apply_turnover_gate",
    "build_target_weights", "can_route_orders", "handlebar", "history_values", "init",
    "momentum_diagnostics", "one_way_turnover", "rank_candidates", "signal_history",
]
