---
name: qmt-agent-strategy-lab
description: 用于 QMT、MiniQMT、XtQuant 的 AI 辅助策略研发、回测、参数稳健性检验和安全部署。用户提到 QMT/MiniQMT 写策略、调试模拟盘、批量试验、回测或把 AI 接入量化交易时使用；默认只生成和验证代码，不发送订单。
---

# QMT Agent Strategy Lab

## 目标

把“研究假设 → 可复现数据切分 → 策略代码 → 本地代理回测 → QMT 原生编译/回测 → 证据报告”串成一个可审计流程。策略必须区分 QMT 客户端原生统计和外部代理统计，所有实盘路由默认关闭。

## 使用前的边界

- 不代填密码、验证码或授权信息；不通过 Computer Use 操纵登录流程。
- 未获得用户对具体账户、标的、金额和订单类型的明确确认时，不调用 `passorder`，不执行模拟盘或实盘下单。
- 保留用户已有文件和账户配置；先读后改，输出写到用户指定目录或工作区 `outputs/`。
- QMT 编辑器常见嵌入式 Python 3.6.x。不要把本机 Python 3.13 的二进制扩展直接复制到 QMT；先做导入/ABI 检查。
- `ENABLE_ORDERS=False`、精确 live token、有效账户白名单、待成交订单 fail-closed、`quickTrade=0` 应作为默认护栏。只有用户明确授权并完成二次检查，才允许改动护栏。
- 把“委托函数已调用”“委托已受理”“成交已确认”作为三种状态；策略状态只能由成交回报或下一次账户对账确认，不能仅凭 `passorder` 返回值更新。

开始工作前读取 `references/qmt-research-patterns.md`，了解已验证的官方接口、开源项目和本 skill 的取舍。

## 标准工作流

### 1. 盘点环境和数据

记录 QMT 客户端路径、嵌入 Python 版本、本机主 Python、xtquant 版本、数据源、时区、交易日历和账户状态。为每个实验写入 source path、文件哈希、参数、数据区间和依赖版本。数据不可用时明确标记 `blocked`，不要以静态文件存在代替可运行性。

优先使用 QMT 原生历史数据接口（例如 `get_history_data` 或 `xtdata.get_market_data_ex`），并在本机代理回测中保存原始数据快照或获取时间。QMT 编辑器代码尽量依赖标准库，避免在嵌入式环境强制导入 pandas/numpy。

### 2. 设计研究切分

先定义基准、交易成本、滑点、调仓频率、持仓上限和风险退出规则，再切分 train/validation/OOS；至少做滚动 walk-forward。信号只能使用当根及以前的数据，成交按下一交易时点模拟。对参数网格记录全部候选，不只保存最优单点。

多因子股票模型应优先采用：横截面排序或 z-score、MAD 去极值、行业/市值中性化（数据允许时）、ST/退市/上市天数/涨跌停过滤、T+1 约束。ETF 轮动应包含风险/防守资产、趋势或动量窗口、最低得分门槛、换手和回撤护栏。

### 3. 生成 QMT 原生策略

保持 QMT 可识别的 `init(C)`、`handlebar(C)` 结构；在 `init` 设置 universe，在 `handlebar` 只做增量计算和受控调仓。策略名称必须稳定，便于 QMT 按策略区分委托和成交。

建议的订单门控结构：

```python
ENABLE_ORDERS = False
BACKTEST_ORDERS = True
LIVE_TOKEN = ""
LIVE_TOKEN_REQUIRED = "I_UNDERSTAND_LIVE_TRADING"

def can_route_orders(account, is_backtest=False):
    if is_backtest:
        return BACKTEST_ORDERS
    return ENABLE_ORDERS and LIVE_TOKEN == LIVE_TOKEN_REQUIRED and account in VALID_ACCOUNTS
```

下单前还要检查有效账户、目标数量、最小成交单位、涨跌停/停牌、未完成委托和资金/持仓；任一检查失败就跳过并记录原因。默认把 `quickTrade=0` 传给订单接口，避免意外即时触发。

### 4. 静态检查和本地代理回测

先运行：

```powershell
python scripts/validate_qmt_strategy.py path\to\strategy.py --json-out outputs\validation.json
```

检查失败时先修复，不进入 UI。代理回测至少输出累计收益、年化收益、最大回撤、波动率、Sharpe、换手/交易次数、基准差额和每个 OOS fold。任何“最佳参数”都要同时展示全样本、训练集、验证集和 OOS，避免把单次优化结果当成稳健结论。

对于多个 ETF 行业/风格候选，优先使用 `scripts/run_batch_research.py`（若已安装）做纯代码并行研究；它从脱敏 OHLCV cache 读取数据，跑多个信号周期和滚动窗口，按训练/验证选参并锁定 OOS。`examples/etf_universe.json` 只提供候选代码与筛选阈值，必须在 QMT 本地再次核对上市、流动性、权限和数据覆盖。批量结果中的 `orders_sent=false` 是硬性证据字段。

### 5. QMT 客户端验收

通过 Computer Use 时遵循“一次观察 → 一次动作 → 刷新观察”。只操作策略导入、编辑、编译、回测和停止；不输入凭据，不点击买卖/撤单。记录可见证据：策略树名称、编译结果、基准、回测是否触发、结果窗口/日志是否出现。没有 QMT 原生收益面板时，报告必须写“原生统计未确认”，并链接到代理回测报告。只有券商 QMT 模拟账户的原生成交、净值和回撤证据才算“QMT 模拟盘验证”；内存模拟和 fake SDK 只证明管线。

### 6. 迭代与晋级

把每次尝试保存为独立 artifact（策略源码、参数 JSON、验证 JSON、回测报告、日志和时间戳）。评审顺序是：先看 OOS 与 fold 一致性，再看回撤/换手/成本敏感性，最后才看收益。训练集优秀而 OOS 失败的模型降级为研究候选，不得部署。

## 交付清单

- 策略源码及其绝对路径、哈希和运行 Python 版本。
- 数据来源、时间范围、基准、成本/滑点和切分方式。
- 本地代理指标与 QMT 原生 UI 证据分栏展示。
- 失败尝试、阻塞原因和下一步，不隐去异常。
- 订单护栏状态；若仍为 `ENABLE_ORDERS=False`，明确说明没有任何委托发送。

## 资源

- `references/qmt-research-patterns.md`：官方接口和 AI+QMT 项目调研摘要、可借鉴模式及本地决策。
- `scripts/validate_qmt_strategy.py`：无网络、可重复的 QMT 策略护栏和语法检查器。
