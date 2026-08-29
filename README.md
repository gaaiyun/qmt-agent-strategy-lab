<p align="center">
  <img src="assets/qmt-agent-strategy-lab-logo.svg" width="180" alt="QMT Agent Strategy Lab 项目标识">
</p>

<h1 align="center">QMT Agent Strategy Lab</h1>

<p align="center">面向 QMT、MiniQMT 与 XtQuant 的可审计策略研发 skill。</p>

<p align="center">
  <a href="https://github.com/gaaiyun/qmt-agent-strategy-lab/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/gaaiyun/qmt-agent-strategy-lab/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://www.python.org/"><img alt="Python 3.9+" src="https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-2ea44f.svg"></a>
</p>

给 Coding Agent 使用的 QMT 研究流程、ETF 行业候选池、量价多因子模板、纯代码批量回测器和安全护栏检查器。默认只研究和回测，不登录、不代填凭据、不发送订单。

> [!IMPORTANT]
> 静态检查和代理回测不能证明 QMT 客户端当前可用，也不能保证未来收益。只有 QMT 原生回测面板或模拟账户的可核对成交/绩效证据，才属于对应层级的证据；任何结果都不构成投资建议。

## Quick Start

```powershell
git clone https://github.com/gaaiyun/qmt-agent-strategy-lab.git
cd qmt-agent-strategy-lab
python --version  # Python 3.9+
```

检查 QMT 策略：

```powershell
python scripts/validate_qmt_strategy.py examples/qmt_etf_rotation_live.py --json-out out/validation.json
```

运行本地并行研究（需要一个包含日期、OHLCV 的 JSON cache；不包含账户信息）：

```powershell
python scripts/run_batch_research.py --cache path/to/yahoo_etf_cache.json --workers 4 --output-dir out
```

脚本会输出训练/验证/OOS、1 年/2 年/3 年/早期滚动窗口、交易次数和成本假设。候选只按训练与验证排序，OOS 锁定后才读取。

## 这个 skill 做什么

| 模块 | 作用 | 证据边界 |
|---|---|---|
| [`SKILL.md`](SKILL.md) | Agent 路由、工作流和订单安全边界 | 规则本身不验证平台运行 |
| [`references/qmt-research-patterns.md`](references/qmt-research-patterns.md) | 官方接口与公开项目的调研摘要 | 版本和券商实现可能变化 |
| [`examples/qmt_etf_rotation_live.py`](examples/qmt_etf_rotation_live.py) | 风险 ETF/防守腿轮动模板 | 必须在本机 QMT 编译、回测 |
| [`examples/qmt_multifactor_live.py`](examples/qmt_multifactor_live.py) | ETF 横截面量价多因子模板 | 不是股票基本面多因子；数据字段可得性需确认 |
| [`scripts/run_batch_research.py`](scripts/run_batch_research.py) | 纯代码、进程并行、多周期研究 | 代理成交模型，不是券商成交 |
| [`scripts/validate_qmt_strategy.py`](scripts/validate_qmt_strategy.py) | AST 语法、入口、订单门控和 quickTrade 检查 | 不能证明没有未来数据或能够成交 |
| [`examples/etf_universe.json`](examples/etf_universe.json) | broad/style/industry/overseas/commodity 候选池 | 代码、权限、流动性必须在 QMT 中二次核对 |

## ETF 行业池与使用方式

候选池按经济暴露分为 broad、growth、technology、healthcare、consumer、finance、cyclical、industrial、overseas、commodity 和 defensive。核心池每个 sleeve 先选代表性标的，再用上市天数、成交额、缺失率、零成交日和相关性过滤；行业 ETF 是研究对象，不是买入推荐。

建议流程：

1. 在 QMT/交易所行情中确认代码、上市日期、可交易状态和最小单位。
2. 过滤停牌、涨跌停不可成交、历史不足和成交额不足的标的。
3. 计算多周期动量、趋势、成交量活跃度、波动率和回撤；按风险预算配置，剩余资金进入防守腿。
4. 用滚动 OOS 和成本压力检验筛选参数；同一参数再导入 QMT 做原生编译/回测。

公开代码/名称只作为候选池来源，需以本机 QMT 行情和交易权限为准。可回查 [上交所基金公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-19/512720_20260319_B8RP.pdf)、[深交所投资者页面](https://investor.szse.cn/knowledge/t20220718_594830.html) 和 [上交所 ETF 产品资料](https://etf.sse.com.cn/fund/learning/download/c/10055616/files/957e25b81e01425aa4413264089f28d5.pdf)。

## 多因子模型边界

当前模板是“ETF 横截面量价多因子”：短/中/长动量、趋势、上涨广度、成交量相对活跃度、低波动和回撤惩罚，并执行 MAD/分位数式的边界控制、最大持仓数、单标的上限、目标波动率和防守残余。它不是股票基本面模型。

如果要做股票级基本面多因子，应另外提供带日期的 `pe_ttm`、`pb`、`roe_ttm`、盈利增长、流动性和行业/市值字段，并记录公告可得时点；缺字段时必须 fail-closed，不能用未来数据或零值占位。行业/市值中性化、ST/退市/上市天数过滤和 T+1 约束应在数据适配器中明确实现。

## QMT 兼容与安全

- QMT 编辑器常见内置 Python 3.6.x；本地 Python 3.9+ 只用于离线检查和研究。不要把 3.13 的二进制扩展复制进 QMT。
- 示例保持 `ENABLE_ORDERS = False`，live 路由还需要精确确认串、账户白名单、资产/持仓查询和活动委托 fail-closed。
- `passorder` 的返回值只表示请求进入处理流程，不等于成交；状态必须由成交回报或下一次账户对账确认。
- `quickTrade=0` 用于让历史回测逐 K 线执行；策略代码只在 QMT 端负责信号和受控调仓。
- 本仓库不保存券商账号、密码、Cookie、token、本地路径或 Yahoo 数据快照。

## 验证与证据分层

```powershell
python -m unittest discover -s tests -p 'test_*.py'
python scripts/validate_qmt_strategy.py examples/qmt_multifactor_live.py
python scripts/validate_qmt_strategy.py examples/qmt_etf_rotation_live.py
```

| 层级 | 可以说明 | 不能说明 |
|---|---|---|
| 单元测试 / AST | 代码结构和已知护栏通过 | QMT 云端行为、未来数据安全、收益 |
| 本地代理回测 | 成本、下一开盘、OOS 和滚动窗口下的研究结果 | 券商撮合、真实成交、未来盈利 |
| QMT 原生编译/回测 | 指定客户端能执行指定模型 | 模拟盘长期表现或实盘适用性 |
| 模拟账户前向记录 | 当前账户与市场条件下的成交和净值 | 长期收益保证 |

## 调研依据

接口层优先回查 [QMT Python API](https://www.miniqmt.com/qmtapi/QMT_Python_API_Doc.html) 和 [miniQMT 示例](https://miniqmt.com/pages/examples/index.html)。公开的 AI/Agent 研究实现包括 [dfkai/xtquantai](https://github.com/dfkai/xtquantai)、[adennng/stock_strategy_lab](https://github.com/adennng/stock_strategy_lab)、[2233admin/qmtcli](https://github.com/2233admin/qmtcli)、[juju-w/qmt-mcp](https://github.com/juju-w/qmt-mcp) 和 [guoyaohua/etf-adaptive-rotation-qmt](https://github.com/guoyaohua/etf-adaptive-rotation-qmt)。本 skill 借鉴其可复现研究、JSON CLI、对账和人工确认边界，不复制未经验证的参数或下单权限。

## 项目结构

```text
qmt-agent-strategy-lab/
├── README.md
├── SKILL.md
├── agents/openai.yaml
├── references/qmt-research-patterns.md
├── scripts/validate_qmt_strategy.py
├── scripts/run_batch_research.py
├── examples/qmt_*_live.py
├── examples/qmt_backtest_research.py
├── examples/etf_universe.json
├── tests/test_batch_research.py
└── .github/workflows/ci.yml
```

## License

MIT，见 [`LICENSE`](LICENSE)。
