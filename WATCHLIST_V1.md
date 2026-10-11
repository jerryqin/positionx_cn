# PositionX Watchlist Engine v1 — Research Core

本阶段只解决一个问题：

> 已完成日K + 当前持仓 + 人工固定关注
> → 自动候选、自选股排名、入选/未入选原因

## V1 自动筛选

普通候选需要满足：
- 日K数量足够（默认 >=60）
- 最低价格
- `close > MA20 > MA60`
- 20日收益达到最低要求
- 距20日高点不远
- 20日平均成交额达到最低要求

排名分数（0~100）：
- 趋势 35
- 20日动量 20
- 接近20日高点 15
- 流动性 10
- 横截面20日相对强度 20

这个 score 只是筛选/排序分，不是收益预测。

## 强制保留

`Watchlist = 自动候选 + 当前持仓 + 人工固定关注`

持仓即使筛选失败、甚至缺少历史数据，也不会从 Watchlist 消失；
`failed_reasons` 会明确说明原因。

## 安全边界

`research/watchlist.py` 不依赖：
- broker
- RiskGate
- Execution
- TradingPlanItem
- 生产 SQLite

因此可以在 Pilot SELL 尚未完成时开发，不会影响 v1.0.14b 实盘基线。

## 安装

将以下文件复制到 `D:\positionx_min_live` 对应目录：
- `research/__init__.py`
- `research/watchlist.py`
- `tests/test_watchlist.py`

然后运行：

```powershell
python -m unittest discover -s tests -p "test_watchlist.py" -v
python -m unittest discover -s tests -v
```

本包新增 5 个测试。

## 下一步

下一层只做一个薄适配器：
1. 从 miniQMT 获取一个可配置 Universe（先从沪深300验证）；
2. 自动合并真实持仓；
3. 通过现有 MarketDataService 读取完成日K；
4. 调 WatchlistEngine；
5. 只输出 research-side JSON/SQLite 快照；
6. 绝不创建 TradingPlanItem。

这层稳定后才做 Strategy Matching。
