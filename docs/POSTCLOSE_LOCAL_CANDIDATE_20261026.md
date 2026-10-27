# 盘后本地研究候选（2026-10-26）

本次方向以用户明确指令为准：取消 Full-QMT 迁移、v03 真实验收和生产接入推进，停止实盘下单、账户及真实持仓查询相关开发，保存既有证据。继续 Deep Oversold、RET120 CSI500 的冻结模型计算，以及 Universe-Aware Funnel 的独立研究。本文记录候选，不安装或改变现有 Scheduler，也不修改旧治理文件中的历史事实。

核验基线：`main`，HEAD `e54a29b0670b8018a3b686f637479a8a97c9a023`。进入会话时已有六个受跟踪文件修改，以及未跟踪 `reports/`；这些既有修改不属于本次交付。

## 直接调用链与处理

| 范围 | 现有耦合 | 候选处理 |
|---|---|---|
| 盘后任务 | `daily_ops_task_registry_v1.json` 中 `A_SHARE_DAILY_UPDATE` 依赖 `BROKER_RECONCILE_PRECLOSE` | 独立入口直接验证已封版本地输入和真正盘前 PIT；不调用旧 orchestrator、不修改该依赖 |
| 交易日 | 旧 orchestrator、RET120 从 QMT 取得交易日历 | 使用已批准、有效期内的 `cn_exchange_calendar_2026.json`；未知日期阻断 |
| Deep | 导入 runner 时立即 `get_xtdata()`；入场时查询 instrument detail | 移除导入期初始化；计算函数接受显式本地 detail 和交易日列表 |
| RET120 | `run_forward` 初始化 xtdata，取日历、指数、证券详情 | 只复用 entry / exit / snapshot / signal 计算；详情取同日封版 identity/status；不调用 `run_forward` |
| 股票数据与指数 | `p0_rq_input.strategy_histories` 使用本地 RQ 股票加 Full-QMT 指数 | 直接调用封版输入的 `DailyInput.histories`；不请求指数、不替换指数，不伪造 benchmark |
| RET120 与 Funnel | 原 `ret120_forward` handler 先运行全市场 prior probe / refresh / capture | 候选分别计算 CSI500 模型与全市场 prior；Funnel 的缺口不阻断两个核心模型 |
| Funnel | prior 可刷新 QMT；structure 使用 `market.daily_bars_v1` | 不调用 refresh/capture；复用冻结计算及分层覆盖门槛；原结构数据过期则只输出缺口 |
| 研究账本 | ResearchStore 默认可写，生产新段仍停留在 10 月 22 日游标 | 只读检查原账本策略指纹，复制到独立输出后事务计算；不补造 `forward_sessions` |
| 日报 | 原日报读取整条旧任务链，并含真实账户字段 | 新日报保留所引用的历史任务状态及文件哈希，真实账户字段写 `null / UNKNOWN_NOT_QUERIED` |
| 组合 | 原 recovery portfolio 要求当日 Forward session / 指数证据 | 复用冻结 50/50 初始资本分袖计算，输出独立模型组合，原 Portfolio 验收不变 |

## 精确源码范围

- 修改 `scripts/run_deep_oversold_csi500_forward.py`：把 xtdata 获取延至旧数据函数被实际调用时；为 `process_entries` 增加本地证券详情与交易日列表参数，为 `process_exits_and_mark` 增加交易日列表参数。默认调用方式继续可用；新候选不进入那些 SDK 数据函数。
- 修改 `scripts/run_ret120_csi500_forward.py`：为 `process_entries` 和 `detect_signals` 增加本地证券详情参数；未提供时保留原路径。未修改信号、成本、入场、退出、排序和容量定义。
- 新增 `scripts/run_postclose_local_candidate.py`：本地输入核验、隔离复制、三策略直接计算、模型导出、组合和日报。
- 新增 `tests/test_postclose_local_candidate.py`：隔离边界、日期重复/缺口、未知状态、账本指纹、Deep 整手/成本/S20、RET120 原始价与研究单位/S120 延期退出。
- 修改 `tests/test_p0_rq_input_v01.py` 的 fixture：使用自动清理临时目录，并为 Deep 惰性数据获取注入 fake runtime；不放宽断言。
- 新增本文；结果只写 `reports/postclose_local_*`。

未修改 Broker、main、Full-QMT bridge、SDK、冻结策略配置、现有调度源码或生产配置；未修改历史验收和预期哈希。保留旧模块不代表继续其接入开发。

## 运行与语义

在仓库根目录，用已有虚拟环境运行，输出目录必须不存在：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/run_postclose_local_candidate.py --date 20261023 --output reports/postclose_local_20261023_review
```

不设置或持久化环境变量。`-B` 防止生成 Python 缓存；`-X utf8` 只影响本次进程文本编码。入口拒绝 SDK/Broker 模块导入、网络、子进程和输出目录外的 Python 文件写入；原数据库全部以只读连接检查，只有显式复制的 `model.duckdb` 可写。DuckDB 原生写入不依赖 Python audit hook，而由连接路径隔离；该边界不声称是操作系统沙箱。

本次使用实际留存的 `asof_20261023_e2074bcd5104` 股票资产、同日 08:20 采集的真实盘前成分股 PIT，以及 start_date 为 10 月 23 日的两个新段原账本。输入必须通过 manifest/COMPLETE/数据库哈希、质量状态、复权兼容证据、精确日期及 PIT 时点/数量/不重叠校验；RET120 保留 400/500 门槛，Funnel 保留各层 80% 和最少 50 的门槛。无数据不回退到邻近日期或真实 SDK。

这次是事后隔离复算，不是补录历史盘前证据或历史 Forward 样本。复制账本使用 `ISOLATED_MODEL_REPLAY_NOT_FORWARD` 状态与 `postclose_model_sessions` 表；不写成功 `forward_sessions`，不推进原 review gate，不导入真实账户数据。`plan.csv` 只表示模型待入场意图，未来开盘价、数量及真实可成交性不能据此确认。Deep 使用整手研究单位；RET120 使用冻结碎股研究单位，两者均不是券商持仓或成交。

本地股票资产无 CSI500 指数行情，因此本次保留指数比较和原 Forward session 验收缺口。Funnel 结构及风险预览使用的原市场目录只到 2026-10-15；没有把其他来源的金额/成交量单位静默替代进去。Funnel 退出未资格化，不产生该策略模型持仓、退出或组合份额。

`report.json` 提供三策略结果、覆盖、来源哈希和所引用历史失败记录；`report.md` 为本次盘后日报。进程返回 0 仍可能是 `PARTIAL_RESEARCH_CANDIDATE`，需读业务状态；阻断输出或边界失败返回 3。输出目录不可复用，从而避免覆盖已有证据。模型账本日期必须紧接源游标，重复或跳日阻断。

## 有限验收及回退

验证重点是无真实 SDK / 外部查询、源资产不变、冻结模型规则不变，以及不足信息如实阻断。使用临时假账本验证实际入场和退出；本地真实输入首次模型日原账本为空，不能从该日没有模型成交推出实际账户没有成交。

测试范围为新增候选、Deep 和 RET120 策略、Funnel、universe prior、Portfolio，以及 Deep 原运行保护的静态测试。未运行整个 tests 目录，未触发生产运行。全仓 diff 检查还需区分进入会话前已有改动与本次源码差异，不自动修复用户原有 CRLF/行尾问题。

回退：停止使用新增候选入口和隔离产物即可；没有安装步骤或生产数据回滚。两个 runner 的小范围接口改动保留在工作区供审阅，未暂存、未提交或推送。无需删除历史证据。

## 本次实际结果

最终输出位于 `reports/postclose_local_20261023_20261026_verified/`，业务状态为 `PARTIAL_RESEARCH_CANDIDATE`。Deep 输出 37 条信号及模型待入场计划；RET120 的 500/500 特征有效，Q5 输出 100 条计划。两者源新段均为空，因此本次模型持仓、模型退出均为 0。50/50 组合在首个共同模型日为 100 万模型权益，不代表真实净值或盈利。

Funnel 四层有效数量为 CSI300 300/300、CSI500 500/500、CSI1000 999/1000、EX_CSI1800 3380/3425，均达到冻结门槛；动量 prior 候选 360 个、均值回归 prior 候选 676 个。结构预览仍因原目录只到 10 月 15 日而阻断；退出仍未资格化。

69 项针对性测试通过：新增候选 8、RET120 11、Deep 8、Funnel 5、Deep 原运行保护静态测试 5、Universe Prior 6、Portfolio 10、选定 RQ 契约 16。未运行整个测试目录。本次改动范围的 diff 检查通过；全仓 `git diff --check` 报告进入会话前已有的配置与 ops_runtime CRLF/行尾差异，未改动这些文件。

最终验证引用的 37 个源文件哈希全部保持不变，SDK / 网络 / 子进程 / 越界 Python 写入违规记录为空。具体产物审查及测试数量见 `verification.json`。两个早期开发试跑因改进本地批量读取而中断，原目录保留并标记 `INTERRUPTED`，不作为验收。最终输出没有修改生产数据库、Scheduler、历史运行状态或 Git 提交。
