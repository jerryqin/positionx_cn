# PositionX Isolated ORDER_PREVIEW Test v4

该工具只用于实盘前执行链验证，不修改生产 SQLite，不允许下单或撤单。

## 两种引擎路径

默认路径：

```powershell
python scripts\test_order_preview.py --symbol 603986.SH --target 0.20
```

调用 `execute_plan(..., dry_run=True)`。

最终执行保护路径：

```powershell
python scripts\test_order_preview.py --symbol 603986.SH --target 0.20 --engine-path execute-guard
```

该模式会调用 `execute_plan(..., dry_run=False)`，但隔离配置强制：

- `execution.live_enabled=false`
- `execution.auto_execute=false`
- `broker.submit_limit` 硬禁用
- `broker.cancel` 硬禁用
- 使用临时 SQLite

正确结果应为 `ORDER_PREVIEW`，并显示 `PASS_EXECUTE_GUARD`。如果代码试图触发 broker 写操作，硬保护会立即失败。

## BLOCKED 场景

仍可使用：

```powershell
python scripts\test_order_preview.py --symbol 603986.SH --target 0.20 --block-case kill-switch
python scripts\test_order_preview.py --symbol 603986.SH --target 0.20 --block-case external-order
python scripts\test_order_preview.py --symbol 603259.SH --target 0 --block-case no-sellable
python scripts\test_order_preview.py --symbol 603986.SH --target 0.20 --block-case no-cash
```

v1.0.10 后，BLOCKED 审计会区分 `planned_target_weight` / `effective_target_weight`，并区分 `requested_volume` / `allowed_volume` / `volume`。
