# QMT / MiniQMT / AI+QMT 调研摘要

本文件记录 2026-08-29 这一轮调研中可复用的公开模式。链接用于回查原始项目；项目代码和接口会变化，使用前仍要在本机版本上验证。

## 官方与接口层

- [QMT Python API 文档](https://www.miniqmt.com/qmtapi/QMT_Python_API_Doc.html)：覆盖历史数据、模型编辑/回测、交易和风控；`passorder` 的 `strategyName` 用于区分策略委托，`quickTrade=0` 不做即时触发。我们采用稳定策略名和 `quickTrade=0`。
- [miniQMT 示例](https://miniqmt.com/pages/examples/index.html)：包含账号查询、行情和环境检测示例。我们只借鉴调用形态，不把示例凭据写入代码。
- [miniqmt API 兼容性测试](https://github.com/chengjon/miniqmt)：展示不同 xtquant/MiniQMT 版本的函数可用、部分可用和阻塞状态。我们把版本矩阵和“未验证”作为交付字段。

## AI 驱动的研究/代码生成

- [dfkai/xtquantai](https://github.com/dfkai/xtquantai/releases/tag/v0.2.0)：将 QMT 量化 Agent 拆成 skills，首个 `qmt-inner-backtest` 强调 `after_init` 预计算、`handlebar`、MAD 去极值、行业/市值中性化、横截面 z-score、T+1 和 ST/涨跌停过滤。我们把这些作为多因子候选模板，但不假设数据字段一定存在。
- [adennng/stock_strategy_lab](https://github.com/adennng/stock_strategy_lab)：持久化 Agent、研究文件、回测/批评/优化循环，以及 train/validation、walk-forward、基准和阶段归因。我们采用“每次尝试独立 artifact + OOS 优先”的实验账本。
- [juju-w/qmt-mcp](https://github.com/juju-w/qmt-mcp)：通过 MCP 给 Agent 提供券商无关的行情/账户服务。我们不默认安装或连接它；只保留机器可读适配层的设计方向。
- [2233admin/qmtcli](https://github.com/2233admin/qmtcli)：用稳定 JSON CLI 适配 QMT/XtQuant，并用 fake xtquant 做自动化测试。我们借鉴“命令与结果可记录”的思路，不把 fake 测试当成真实成交。
- [atorber/qmt-trading-skill](https://github.com/atorber/qmt-trading-skill)：QMT Bridge 与 Agent Skills 的组合。适合作为未来桥接层参考，当前仍保持本地、人工可见和订单关闭。
- [juju-w/qmt-mcp](https://github.com/juju-w/qmt-mcp)：近期版本把行情、研究数据和账户查询作为只读 Agent 能力，明确不暴露下单/撤单，并要求人工登录。我们采用“研究与账户只读默认开放、交易写操作不进入 Agent 工具面”的边界。
- [beamof/qmt-mcp-server](https://github.com/beamof/qmt-mcp-server)：交易工具需要独立口令，且口令格式不写入提示词或工具描述。我们进一步要求口令、账户白名单和 `ENABLE_ORDERS` 三重门控。

## ETF 与生产安全

- [guoyaohua/etf-adaptive-rotation-qmt](https://github.com/guoyaohua/etf-adaptive-rotation-qmt)：确定性信号、次日执行、账户账本/对账和 `qmt.allow_live_orders:false` 默认值。我们采用风险/防守资产、最低得分门槛、换手控制和实盘默认关闭。
- [zhangsensen ETF rotation](https://github.com/zhangsensen/ETF%E8%BD%AE%E5%8A%A8%E7%AD%96%E7%95%A5)：展示使用 QMT 数据的 ETF 轮动思路；只借鉴“信号与执行分离”，不复制未经验证的参数。
- [Liu-Song-DTC/miniQMT](https://github.com/Liu-Song-DTC/miniQMT)：生产实现强调成交回报、启动对账、FIFO 账本、委托超时、停牌/涨跌停防护和自动交易总开关。我们把“委托已提交”和“成交已确认”分开，重启后以账户查询重建状态。

## 本地决策（借鉴 / 不借鉴）

| 主题 | 借鉴 | 明确不借鉴 |
|---|---|---|
| 因子研究 | 横截面标准化、MAD、T+1、过滤器、滚动 OOS | 只报全样本最优点、未来函数、无成本回测 |
| ETF 轮动 | 风险/防守资产、趋势窗口、次日执行、对账 | 把单个强势区间的高收益当稳健结论 |
| AI Agent | 持久化实验账本、critic、机器可读结果 | 让 Agent 直接获得下单权限 |
| QMT 适配 | 原生数据接口、稳定 strategyName、版本矩阵 | 把 Python 3.13 扩展复制进 QMT Python 3.6 |
| Computer Use | 导入/编译/回测/停止的可见验收 | 输入密码、点击买卖/撤单、无证据声称已成交 |
| 订单状态 | 成交回报、启动对账、未完成委托去重 | 把 `passorder` 返回或按钮点击直接当成成交 |
| 研究选择 | 训练+验证选参，最终测试锁定，参数邻域与成本压力 | 使用最终测试集挑“最佳参数” |

## 本项目的验收口径

1. 语法和护栏检查通过；`ENABLE_ORDERS=False` 时不得产生委托。
2. 本地代理回测显示 train/validation/OOS、滚动 fold、回撤、换手和成本假设。
3. QMT 原生结果只有在客户端明确出现绩效/成交面板或对应日志时才标记为已确认；仅“编译成功/回测按钮触发”标记为部分证据。
4. 研究候选晋级需 OOS 不依赖单一 fold，且压力成本后仍符合用户设定的回撤和换手约束。
5. “QMT 模拟盘验证”必须来自券商 QMT 模拟账户的原生成交与绩效证据；内存模拟、fake xtquant、外部 Yahoo 代理回测均不能替代。
