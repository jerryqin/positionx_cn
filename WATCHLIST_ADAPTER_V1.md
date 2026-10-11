# Watchlist Data Adapter v1

新增：
- `research/watchlist_adapter.py`
- `scripts/run_watchlist.py`
- `tests/test_watchlist_adapter.py`

不修改任何 `core/` 文件。

## 安全设计

运行脚本时：
1. 从正式 `settings.yaml` 读取 miniQMT 账户配置；
2. 强制改用临时 SQLite，生产 DB 不打开；
3. 强制 `live_enabled=false / auto_execute=false`；
4. 运行时把 `broker.submit_limit` 和 `broker.cancel` 替换为硬拒绝函数；
5. 只调用 `query_positions()` 获取真实持仓；
6. 不创建 `TradingPlanItem`；
7. 唯一持久输出为 `data/research/watchlist_latest.json`。

## 第一次接线验证

先只跑沪深300前30只 + 当前真实持仓：

```powershell
python scripts\run_watchlist.py --sector 沪深300 --sample 30 --refresh-sector --refresh-history
```

关注：
- `production_db_opened=false`
- `broker_submit_guard=ENABLED`
- `combined_symbols`
- `history_loaded`
- `holdings_retained`
- `auto_selected`
- OUTPUT 路径

如果接线正常，再运行全部沪深300：

```powershell
python scripts\run_watchlist.py --sector 沪深300 --refresh-history
```

后续日常运行不必每次刷新板块分类：
```powershell
python scripts\run_watchlist.py --sector 沪深300
```
