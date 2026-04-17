# Automated Trading Roadmap

**目标**: 基于 miniqmt-cli + trading-analysis 构建完备的 A 股自动化交易系统

**约束**: Mac 驱动，Windows 执行（xtquant），SSH 隧道连接

---

## 当前已完成

| 组件 | 版本 | 能力 |
|------|------|------|
| miniqmt-cli | v0.1.0 | 行情查询、实时推送、账户管理、下单/撤单、部署向导 |
| trading-analysis | v0.1.0 | 资金流向分析（历史+实时）、信号表达式引擎、MA 均线 |

---

## Phase 1: 订单生命周期闭环

**目标**: 下单后能实时追踪订单状态，策略可根据成交反馈做决策

### 1.1 订单状态推送 (miniqmt-cli)

xtquant 的 `XtQuantTrader` 提供异步回调:
- `on_order_stock_async_response` -- 下单回执
- `on_order_event` -- 订单状态变化（已报、已成、部成、已撤、废单）
- `on_trade_event` -- 逐笔成交回报

daemon 端需要:
- 注册 xttrader 回调，转发到 SSE 端点 `/stream/order`
- 客户端 `miniqmt-cli stream order --account sim` 消费事件

### 1.2 下单返回增强 (miniqmt-cli)

当前 `order buy/sell` 只返回 `{seq, status}`。增强为:
- 同步等待回执（带超时）: `--wait 5` 等待最多 5 秒拿到委托确认
- 返回字段增加: order_id, order_status, filled_volume, avg_price

### 1.3 成交汇总查询 (miniqmt-cli)

- `account fills --account sim --order-id 12345` -- 查某笔委托的逐笔成交明细
- 补充 `account orders` 返回字段: status, filled_volume, avg_price

**交付标准**: 从下单到全部成交，每个状态变化都能实时感知

---

## Phase 2: 风控层

**目标**: 防止策略失控导致重大损失

**状态**: 已完成 (v0.2.0, 2026-04-17)

### 2.1 daemon 端风控 (miniqmt-cli server)

在 `/trade/order` 请求管道中插入风控检查，独立于 CLI 端守卫:

- **单日亏损限制**: 当日已实现亏损 + 浮动亏损超过阈值，拒绝新开仓单
- **持仓集中度**: 单股持仓市值不超过总资产 X%
- **下单频率**: 滑动窗口内最多 N 笔委托（防循环下单 bug）
- **最大持仓数**: 同时持有不超过 N 只股票

配置在 `server.toml` 的 `[risk]` 段:

```toml
[risk]
max_daily_loss = 50000          # 单日最大亏损(元)
max_position_pct = 30           # 单股最大持仓占比(%)
max_orders_per_minute = 10      # 每分钟最大委托数
max_positions = 10              # 最大持仓股票数
enabled = true
```

### 2.2 熔断机制

- 触发风控后自动进入「只撤不开」模式
- daemon 健康端点返回风控状态: `{"state": "risk_breaker_triggered"}`
- 手动解除: `miniqmt-cli risk reset --account sim --confirm-live XXXX`

**交付标准**: 风控独立于策略运行，即使策略代码有 bug 也不会无限亏损

---

## Phase 3: 条件单引擎

**目标**: 在 daemon 端实现常用条件单，不依赖 Mac 端在线

### 3.1 基础条件单 (miniqmt-cli server)

daemon 端维护条件单列表，轮询行情触发:

- **止损单**: `miniqmt-cli order stop-loss --account sim --code 002028.SZ --trigger-price 200 --volume 100`
- **止盈单**: `miniqmt-cli order take-profit --account sim --code 002028.SZ --trigger-price 250 --volume 100`
- **价格触发单**: `miniqmt-cli order trigger --account sim --code 002028.SZ --condition "price <= 200" --side sell --volume 100`

### 3.2 追踪止损

- `miniqmt-cli order trailing-stop --account sim --code 002028.SZ --trail-pct 5 --volume 100`
- daemon 持续跟踪最高价，回撤超过 trail_pct 触发卖出

### 3.3 条件单管理

- `miniqmt-cli order pending --account sim` -- 查看所有挂起的条件单
- `miniqmt-cli order cancel-trigger --id <trigger_id>` -- 取消条件单

**交付标准**: Mac 断网后，daemon 仍能执行止损/止盈

---

## Phase 4: 通知推送

**目标**: 关键事件实时推送到手机

### 4.1 Webhook 框架 (miniqmt-cli server)

`server.toml` 配置:

```toml
[notify]
webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
events = ["signal_triggered", "order_filled", "risk_breaker", "stop_loss_triggered"]
```

### 4.2 事件类型

| 事件 | 触发时机 | 推送内容 |
|------|---------|---------|
| order_filled | 委托成交 | 代码、方向、成交价、成交量 |
| risk_breaker | 风控熔断 | 触发原因、当日损益 |
| stop_loss_triggered | 止损触发 | 代码、触发价、止损价 |
| signal_triggered | 策略信号 | 代码、信号表达式、变量值 |
| daily_summary | 每日收盘后 | 当日损益、持仓汇总 |

**交付标准**: 人不在电脑前也能第一时间知道关键事件

---

## Phase 5: 策略框架

**目标**: 从「信号表达式」升级为可编程策略

### 5.1 策略定义 (trading-analysis)

策略是一个 Python 文件，放在 `strategies/` 目录:

```python
# strategies/ma_cross.py
from trading_analysis.strategy import Strategy, Context

class MaCross(Strategy):
    name = "ma_cross"
    codes = ["002028.SZ"]
    interval = 10  # 秒

    def on_tick(self, ctx: Context):
        if ctx.ma(5) > ctx.ma(20) and ctx.main_net > 0:
            ctx.buy(volume=100)
        if ctx.ma(5) < ctx.ma(20):
            ctx.sell_all()
```

### 5.2 策略运行器

```bash
# 前台运行（调试用）
trading-analysis run --strategy strategies/ma_cross.py

# 后台 daemon 化
trading-analysis run --strategy strategies/ma_cross.py --daemon

# 查看运行中的策略
trading-analysis strategy list

# 停止策略
trading-analysis strategy stop ma_cross
```

### 5.3 策略状态持久化

- 策略状态（持仓、信号、累计 PnL）写入 `~/.trading_analysis/state/<strategy>.json`
- daemon 重启后自动恢复
- 支持手动快照: `trading-analysis strategy snapshot ma_cross`

**交付标准**: 策略可 7x24 无人值守运行（交易时间内自动执行，非交易时间休眠）

---

## Phase 6: 回测与验证

**目标**: 上线前验证策略有效性

### 6.1 回测引擎 (trading-analysis)

```bash
# 用历史 ticks 回测策略
trading-analysis backtest --strategy strategies/ma_cross.py \
  --start 20260101 --end 20260416 \
  --initial-capital 1000000
```

输出: 总收益率、最大回撤、夏普比率、胜率、交易明细

### 6.2 模拟盘验证

- `trading-analysis run --strategy strategies/ma_cross.py --paper`
- 接真实行情，模拟下单（不实际提交），记录虚拟损益
- 跑满 N 天后对比策略预期 vs 实际行情

### 6.3 实盘对比

- 模拟盘和实盘并行跑同一策略
- 自动对比差异（滑点、成交率、延迟影响）

**交付标准**: 每个策略上实盘前必须通过回测 + 模拟盘验证

---

## Phase 7: 运维与监控

**目标**: 生产级可靠性

### 7.1 系统监控

- daemon 健康自检 + 自动重启
- xtquant 连接断线重连
- SSH 隧道断线自动恢复（autossh 已支持）

### 7.2 日志与审计

- 所有交易操作写入结构化日志（已有 audit.jsonl）
- 增加: 策略决策日志（为什么买/卖）、风控触发日志
- 日志轮转 + 归档

### 7.3 每日报表

```bash
# 自动生成每日交易报告
trading-analysis report --date 20260417
```

输出: 当日交易汇总、持仓变化、损益归因、风控事件

---

## 里程碑时间线

| 里程碑 | Phase | 交付物 | 状态 |
|--------|-------|--------|------|
| M0: 数据通道 | -- | miniqmt-cli v0.1.0 | **已完成** |
| M1: 资金分析 | -- | trading-analysis v0.1.0 (moneyflow + live + signal) | **已完成** |
| M2: 订单闭环 | Phase 1 | 订单状态推送 + 成交反馈 | 待开发 |
| M3: 安全底线 | Phase 2 | 风控层 + 熔断 | **已完成** |
| M4: 自动执行 | Phase 3+4 | 条件单 + 通知推送 | 待开发 |
| M5: 策略引擎 | Phase 5 | 可编程策略 + 后台运行 | 待开发 |
| M6: 验证体系 | Phase 6 | 回测 + 模拟盘 | 待开发 |
| M7: 生产就绪 | Phase 7 | 监控 + 报表 + 运维 | 待开发 |

---

## 架构演进

```
当前:
  Mac CLI ──tunnel──> Windows daemon ──> xtquant
  trading-analysis ──> miniqmt-cli transport

M4 之后:
  Mac CLI ──tunnel──> Windows daemon ──> xtquant
                          |
                          ├── 条件单引擎 (daemon 内)
                          ├── 风控层 (daemon 内)
                          └── Webhook 推送

M5 之后:
  Mac: trading-analysis strategy runner
          |
          ├── 策略 A (ma_cross)
          ├── 策略 B (moneyflow_reversal)
          └── 策略 C (...)
          |
          v
  Windows daemon (执行层, 风控层, 条件单)
          |
          v
       xtquant ──> 券商
```

关键原则:
- **风控和条件单在 daemon 端**: Mac 断网不影响止损执行
- **策略在 Mac 端**: 灵活迭代，Python 生态丰富
- **daemon 是执行层不是决策层**: 保持简单可靠
