# PositionX-CN 项目上下文

核验时间：2026-10-17 14:07，Asia/Shanghai。
本文件是状态快照；长期安全约束以根目录 AGENTS.md 为准。
下次使用前应重新核验 Git、配置与最新运行证据。

证据优先级（由高到低）：
当前源码/配置与当前系统只读状态 >
带时间戳运行证据 >
历史验证包 >
运维文档 >
历史聊天。
发生冲突时不得自动调和，应明确报告并重新核验。

状态标记：
- 【已核验】本次直接读取代码、配置或系统状态确认。
- 【历史证据】本次重新读取了带时间戳的既有结果，未重跑。
- 【判断】基于已核验证据形成的分析、优先级或建议，不是运行事实或执行授权。
- 【待确认】现有证据不足，或需要另行授权的运行验证。

## 1. Git 状态

【已核验：2026-10-17 14:07 Asia/Shanghai】

- branch：main
- HEAD：d33a0dc33b4b93565106d11c110041fd3b58978d
- tree：3be17126d8590c7897eb331ab6713d22104b7f3c
- parent：3bb27e170763962b6cebd25af8641288821384ed
- 最新提交：PositionX-CN: add Codex repository governance
- 提交时间：2026-10-17T14:02:36+08:00
- 工作区、暂存区均干净，git status --short 无输出。

最新提交仅增加项目治理文件，不能视为运行故障已经修复。

## 2. 当前主要 milestone

【已核验】任务注册表仍配置：

- cutover_mode：SHADOW_OPS_5_TRADING_DAYS
- shadow_ops_acceptance_trading_days：5

【历史证据】

- Provider scaffold、服务与 GUI broker read bridge 已有提交。
- M1C3A：readiness snapshot read 接入 provider。
- M1C3B：市场数据语义读取 bridge 已提交。
- M1C4：执行引擎的行情依赖接入 market provider。
- M2：真实 QMT / Broker 只读验证及收尾验证已有 PASS。
- Daily Ops Scheduler cutover 已有 APPLY 和独立验证记录。

【判断】当前工作重点：
恢复 Daily Ops 与 Optimus 日常链路稳定性，形成可复核的 Shadow Ops
验收记录。现有日报尚不满足五个绿色交易日要求。

【待确认】
是否存在尚未纳入本次证据范围的人工验收或后续阶段批准。
不得从历史 M2 PASS 自动推导可以进入 M3 或开启实盘。

## 3. Shadow Ops 状态

【已核验：2026-10-17 14:07 Asia/Shanghai】

已有日报：

| 日期 | overall_status |
|---|---|
| 2026-10-13 | RED |
| 2026-10-14 | RED |
| 2026-10-15 | RED |
| 2026-10-16 | RED |

最新日报记录：

- observed_green_days：空
- ready_for_human_acceptance：false
- automatic_transition_to_m3：false

这些报告包含早期未运行任务和生成时快照，不能简单解释为四次完整验收。
10 月 17 日为周六，日报任务按工作日调度；当天无日报不能直接算漏跑。

代码边界：
scripts/run_daily_ops_report_v1.py 的 shadow_acceptance() 当前按 GREEN
日报计数，未独立证明这些日期连续、均为交易日或满足其他完整验收条件。
后续出现绿色日报时仍需人工核验。

## 4. Daily Ops 当前 blocker

### 4.1 最近交易日：MiniQMT 连接失败

【历史证据：2026-10-16】

- 08:20：健康检查、早盘对账、PIT_PREOPEN 成功。
- 14:50：BROKER_RECONCILE_PRECLOSE 失败。
- 日志定位至 MiniQmtBroker.connect()，返回 rc=-1。
- A_SHARE_DAILY_UPDATE 因前置对账未成功而 BLOCKED。
- Deep、RET120、组合、容量、Pilot readiness 和最终对账继续被阻断。
- 18:00：PIT_POSTCLOSE 成功；后续触发跳过已成功任务。
- 18:55：WATCHDOG_EOD 为 FAILED，业务结果 RED。

已确认阻断链：

BROKER_RECONCILE_PRECLOSE
→ A_SHARE_DAILY_UPDATE
→ DEEP_FORWARD / RET120_FORWARD
→ 组合、容量、预览相关检查和最终对账

【待确认】

- rc=-1 的底层原因。
- 当前 Broker 是否已经恢复连接。
- 独立人工操作是否更新过行情或账本。
- 各数据资产与前向账本的实际截止日期。

不能仅根据编排任务 BLOCKED 断言所有底层数据均未更新。

### 4.2 最新自然日：Optimus 采集超时

【历史证据：2026-10-17】

- 08:30：Current22 调度启动。
- 08:37:19：scheduled 结果为 FAIL。
- 失败子命令：prospective intake daily --days 2，rc=2。
- manifest 记录：
  - 查询 2026-10-16 时，page=8 超时；
  - 查询 2026-10-17 时，page=2 超时。
- 08:45：Daily Ops OPTIMUS_STATUS 为 FAILED。
- OPTIMUS_ACCEPTANCE、OPTIMUS_ATTENTION 因依赖未成功而 BLOCKED。

本次 manifest 中 proposal_count=0，但查询有错误；
不得解释为已完整确认“没有新证据”。

【待确认】
超时属于临时网络问题、上游服务问题，还是需修复采集实现；
当前证据不足以归因于限流、接口变更或解析器缺陷。

## 5. QMT / Broker 只读能力

【已核验：当前源码】

- BrokerProvider 仅定义账户、持仓、订单、成交及外部活动订单读取。
- XtQuantBrokerProvider 将这些查询委托给 MiniQmtBroker。
- TradingService 的主要 broker 查询和 GUI 账户、持仓读取使用 provider。
- 连接生命周期仍由 legacy broker 管理。
- main.py check / reconcile 不能当作纯只读命令：
  它们会连接券商并写入本地运行状态。

【历史证据：2026-10-13 08:46:35】

M2B 状态：
PASS_POSITIONX_CN_QMT_M2B_FULL_QMT_LIVE_READ_ONLY_VALIDATION_V2

当时验证覆盖行情、账户、持仓、订单和成交，记录未执行订单操作。

本次额外核验：

- 原始 M2B result.json 的 SHA256 与配置中的 artifact hash 一致。
- durable evidence 中全部 6 个 source_bindings 与当前文件哈希一致。

因此可以确认历史证据及其声明的源码绑定仍匹配。
本次没有重新连接 Broker/QMT，不能声称当前连接健康。

## 6. MarketData cutover / bridge

【已核验：已迁移部分】

- readiness snapshot 使用 market_provider.get_snapshot()。
- 策略历史读取使用 market_provider.history()。
- 服务及旧 daemon scheduler 的交易时段判断使用 provider。
- scheduler 的完整日 K 判断使用 provider。
- ExecutionEngine 注入 market_provider，覆盖报价、价格限制、
  tick、整手数量等语义方法。

【已核验：仍保留的边界】

- provider 仍是对 MarketDataService / xtquant 的兼容包装。
- prepare_plan() 的 refresh_completed_daily_bar() 仍直接使用 legacy
  MarketDataService，未迁移生命周期刷新。
- history(refresh=False) 当前明确禁止隐式下载和写入日线缓存。
- history(refresh=True) 仍可能更新数据。
- 默认 factory 未注入 raw xtdata backend。
- 显式日期范围、交易日和部分 universe 查询依赖额外 backend。
- 部分 PIT as_of、corporate actions、financials 接口仍明确报能力不足。

【判断】结论：
主要运行读取与执行侧行情依赖已建立 bridge；
不能描述成全量市场数据、刷新生命周期及研究链路已经完成统一切换。

## 7. Provider 边界

【已核验】

core/providers/ 中已存在：

- MarketDataProvider
- BrokerProvider
- ExecutionProvider
- ProviderBundle / build_legacy_xtquant_bundle()
- 对应 XtQuant 适配器与错误类型

重要边界：

- BrokerProvider 是只读接口。
- ExecutionProvider 适配器要求 ApprovalContext：
  approved=true 且 token 非空。
- 该检查不等于已经完成完整审批令牌有效性验证。
- 当前 ExecutionEngine 仍持有 legacy broker，包含直接 submit_limit 路径；
  service 的订单监控也仍有直接 cancel 路径。
- 因此不能把 ExecutionProvider 的审批检查描述为已经覆盖所有执行入口。

core/execution.py 当前哈希仍匹配执行 bridge 测试中的冻结值。
这只是源码一致性核验，本次未运行测试。

## 8. Scheduler 当前状态

【已核验：2026-10-17 14:07 Asia/Shanghai，Windows 只读查询】

共发现 13 个 PositionX 任务，均 Enabled=true、State=Ready：

- 11 个 DailyOps 新任务；
- 1 个 DeepOversold Forward 任务；
- 1 个保留的 Optimus Current22 08:30 任务。

主要调度：

| 环节 | 时间 |
|---|---|
| Preopen | 工作日 08:20 |
| Optimus Current22 | 每日 08:30 |
| OptimusVerify | 每日 08:45 |
| Morning watchdog | 工作日 09:05 |
| G4Open | 工作日 09:20 |
| Preclose | 工作日 14:50 |
| PostcloseCore | 15:35 / 15:45 / 15:55 / 16:05 |
| Deep Forward | 15:45 / 16:00 / 16:15 / 16:30 |
| PostcloseTail | 16:20 / 16:40 / 17:00 |
| Postclose watchdog | 17:10 |
| PIT Postclose | 18:00 / 18:15 / 18:30 |
| Report / EOD watchdog | 18:40 / 18:55 |

除每日 Optimus 两项外，上述任务按工作日触发。
Daily Ops 与 Deep 的 Action 指向当前仓库虚拟环境及 orchestrator。

- Daily Ops / Deep：StartWhenAvailable=false。
- 保留的 Optimus 08:30：StartWhenAvailable=true。
- Optimus 两项最新 LastTaskResult 均为 2。
- 最近 Preclose、PostcloseCore、Deep、Tail 和 watchdog 结果也为 2。
- Preopen、G4Open、PIT Postclose、Report 最近结果为 0。

Ready 仅表示当前任务可被调度，不代表最近业务成功。
本次未触发、启停或修改任何任务，未做完整任务 XML 漂移审计。

代码中还保留 main.py daemon 的 DailyScheduler；
它与 Windows Daily Ops 编排是两个入口，不能混为一套调度。

## 9. 最近关键 PASS / FAIL

| 时间（+08:00） | 结果 | 证据性质 |
|---|---|---|
| 10-13 08:46 | QMT M2B 五域只读验证 PASS | 历史记录，源码绑定本次匹配 |
| 10-13 10:14 | M2 finalization 独立验证 PASS，49 项测试通过 | 历史测试记录 |
| 10-15 20:27 证据包 | Shadow Ops hotfix V3 APPLY PASS，39 项测试通过 | 历史记录，不代表当前 HEAD 已重测 |
| 10-16 09:20 | G4 open check PASS | 运行记录 |
| 10-16 09:38 | Optimus status、acceptance、attention PASS | 重试后的运行记录 |
| 10-16 14:50 | MiniQMT 对账连接 FAIL，rc=-1 | 运行日志 |
| 10-16 18:00 | PIT_POSTCLOSE SUCCESS | 运行记录 |
| 10-16 18:55 | EOD watchdog RED | 运行记录 |
| 10-17 08:37 | Optimus scheduled FAIL，CNINFO 超时 | scheduled 结果及 intake manifest |

本次未运行 Python 测试、Doctor 或业务命令。
历史测试 PASS 不代表当前 HEAD 的全部测试已通过。

## 10. 生产安全开关

【已核验：2026-10-17 14:07 Asia/Shanghai，当前配置】

config/settings.yaml：

- live_enabled=false
- auto_execute=false
- pilot_mode=true
- pilot_max_order_value=50000
- pilot_max_orders_per_day=1

Daily Ops 注册表：

- broker_write=false
- real_order_submit_allowed=false
- automatic_approval_allowed=false
- governed_apply_allowed=false

配置中的 risk.allow_buy / allow_sell=true 不是实盘启用授权。

【待确认】

- 正在运行的进程是否使用其他 --config 或已加载旧配置。
- 数据库中的 Kill Switch 实际状态。
- 当前真实账户、订单及外部交易活动。

日报 auto_live_order_count 当前由报告代码写为固定值 0，
不能把该字段作为真实零订单审计证据。

## 11. 重要证据路径

源码与配置：

- AGENTS.md
- config/settings.yaml
- config/ops/daily_ops_task_registry_v1.json
- config/ops/cn_exchange_calendar_2026.json
- config/foundation/qmt_m2_read_only_evidence_v1.json
- core/providers/
- core/service.py
- core/data.py
- core/execution.py
- scripts/run_daily_ops_orchestrator_v1.py
- scripts/run_daily_ops_report_v1.py
- scripts/run_daily_ops_watchdog_v1.py

运行证据根目录：

- D:\positionx_data\ops_runtime\runs
- D:\positionx_data\ops_runtime\logs
- D:\positionx_data\ops_runtime\watchdog
- D:\positionx_data\ops_reports\daily

关键文件：

- D:\positionx_data\ops_reports\daily\20261016\report.json
- D:\positionx_data\ops_runtime\logs\20261016\BROKER_RECONCILE_PRECLOSE\20261016_BROKER_RECONCILE_PRECLOSE_145000_42d2b9f8_01.log
- D:\positionx_data\ops_runtime\runs\20261017\20261017_OPTIMUS_STATUS_084502_f6460677.json
- D:\positionx_data\research\big_opportunity\optimus\scheduled_current22_v0_7_4\20261017\20261017T083719+0800_scheduled.json
- D:\positionx_data\research\big_opportunity\optimus\prospective_monitor_v0_7_1\20261017\20261017T083022+0800\manifest.json

治理证据根目录：

D:\positionx_data\governance\qmt_migration

其下重要记录：

- m1c3a_guarded_commit\20261012T203549+0800\result.json
- m1c3b_guarded_commit\20261012T213817+0800\result.json
- m1c4_guarded_commit\20261012T221631+0800\result.json
- m2b_full_qmt_live_read_only_validation\20261013T084635+0800\result.json
- m2_finalization_independent_validation\20261013T101440+0800\result.json
- daily_ops_scheduler_cutover_independent_validation\20261013T182225+0800\result.json
- shadow_ops_hotfix_v3\20261015T202715+0800\result.json

## 12. 下一步优先事项

【判断】

1. 排查 MiniQMT rc=-1，明确连接恢复条件及获批后的验证方案。
2. 排查 Optimus 查询超时，确认数据覆盖缺口与受控重试方案。
3. 核对行情、Deep、RET120 和组合账本实际日期，制定恢复计划；
   不得事后补造盘前 PIT 或直接重跑冻结 APPLY。
4. 完善 Shadow Ops 验收口径：交易日资格、最终运行状态、
   日报快照时点，以及是否要求连续绿色日。
5. 明确后续 MarketData 生命周期刷新和执行 provider 迁移范围，
   保留现有冻结语义与审批边界。
6. 同步 ARCHITECTURE / OPERATIONS / RECOVERY 等历史文档。

上述为待规划事项，不构成执行、生产数据写入、
Scheduler 修改、交易或 Git 提交授权。
顺序仅表示当前建议优先级，不构成自动任务队列；
不授权自动推进下一事项。
