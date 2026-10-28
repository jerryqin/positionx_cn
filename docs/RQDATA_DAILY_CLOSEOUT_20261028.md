# RQData 单源日更与发布闭环：2026-10-28 候选核验

## 三策略日常入口收口（2026-10-28 20:28 后）

本节为最新实际状态；以下早先快照与当时阻断保留，不作为当前结论。
28日新发布版本为 `asof_20261028_fed558287c91`，路径
`D:/positionx_data/market/p0_daily_inputs_v01/strategy_versions/` 下同名目录。
资格是显式 `CORE_MODELS_V1`、`CORE_MODELS_PASS_V1`，`full_p0_pass=false`；
原 `DAILY_SIGNALS_V1` 版本与默认完整P0读取范围不变。
数据库SHA256为 `fed558287c91928fab881f0f419690e45928aa94b3a871ded2142ba8d1f2dfc3`。

实际152只模型持仓/待入场执行集合已逐证券验证交易所规则，来源为
`EXCHANGE_RULE_DERIVED`，绑定身份、规则生效日期、原文SHA与实际取得时间。
原5223条 `price_tick/tick_available_at` 缺失仍保留，未知品种、日期越界与来源冲突拒绝。
新分支不替代原RQ事实的同日观测要求，也不把当前规则证据追认成事前可用事实。
冻结半tick、复权、成本、容量与研究单位未改；仿射值2.328与原生昨收2.33分别保留。

| 28日实际完整模型 | 原始候选 | 新待入场 | 当日进入/现持仓 | 未进入原因 | 退出/结算 | 日终模型权益 |
|---|---:|---:|---:|---|---|---:|
| Deep | 54 | 38 | 26 / 26 | 41只不足整手或容量 | 退出函数已评估0笔；结算1行 | 1,002,142.07 |
| RET120 | 100 | 74 | 30 / 30 | 70只无空余席位 | 退出函数已评估0笔；结算1行 | 1,005,142.19 |

两本新模型接续27日隔离研究计划，28日仍为 `NOT_FORWARD`。日报及逐证券清单为
`reports/postclose_daily/20261028_attempt_002/report.md`，旧报告、原账本未覆盖。
新计划只表示下个交易日待判断；未填未来开盘价格、数量或真实成交。
两策略均先处理开盘进入，再处理收盘退出，没有使用收盘卖出资金支持开盘买入。
未来首次真实合格盘后事件可依本次授权从独立空模型账户开始；须有真实当日PIT、
合格输入及实际冻结时刻，不能继承这些历史持仓或计划。目前该自然事件尚未发生。

Funnel保留1036个候选与完整排序：992个结构未就绪、36个有条件风险预览、
6个 `special_type=Other` 分类未知、2个 `SKIP_MIN_ORDER_RISK`。
未知证券为000078.SZ、002731.SZ、300068.SZ、300167.SZ、603398.SH、603922.SH，
当日身份和停牌状态完整，不能按名称补ST判断。
科创板按最低200股、其后1股递增；预算不足不向上加仓。
完整模型为 `FUNNEL_FULL_MODEL_BLOCKED_EXIT_POLICY`，不输出持仓或收益；
唯一推荐补全及仍须批准的成本合同见
[退出差异单](FUNNEL_EXIT_POLICY_DECISION_20261028.md)。

正式入口仍是 `.venv/Scripts/python.exe -X utf8 -B scripts/run_data_only_daily.py --phase postclose`。
本次从该入口实际返回GREEN，两核心模型PASS；同输入重入复用完整报告，31个产物哈希不变，
未重复入账。此次市场认证/API调用均为0。固定状态位于根目录 `RESULT.md`、`DATA_STATUS.csv`
以及 `D:/positionx_data/ops/three_strategy_data_v1/last_run.json`，不宣称发送远程通知。
20:37:13又从已注册的 `PositionX-DataOnly-Postclose` 触发一次受控运行，
20:37:14完成、任务退出0、业务GREEN，确认实际任务入口读取同版并复用已有完整模型。

两个既有任务保持启用，绝对Python及工作目录均为本仓库，下一首触发为
2026-10-29 08:20（盘前）和16:10（盘后），原有有界后续触发与IgnoreNew不变。
实际完整计算约281秒，超过原日报子进程240秒；此次将其限时改为最多600秒，
真实新日期受17:15剩余时间进一步限制，仅将盘后任务总限时由5分钟改为15分钟。
盘前5分钟限时、HTTP240秒总预算、请求上限、安全边界与17:15截止保持；不新增重试。
实际任务XML前后比较仅 `ExecutionTimeLimit` 一项改变。两个阶段均无遗留永久来源阻断。
安装/人工入口成功与未来自然调度成功分开记录，29日运行尚未发生。

源码前像、任务XML前像及单项XML差异位于
`reports/daily_closeout_20261028/TEMP/before/` 及同级运行回执；原始交易所文件保留于
`TEMP/rules/`，不入Git。回退时恢复本次精确源码前像及盘后原XML；新发布版本和研究
产物保留，不改写旧数据或冻结资产。

规则原文为[上交所2026交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)、
[深交所2026交易规则](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)
及[科创板申报数量说明](https://edu.sse.com.cn/tib/qa/c/4761956.shtml)；
规则配置另绑定已取得的官方证券代码范围及特别适用条款，不靠证券名称推断资产类别。

## 28日日线信号研究资格修复（晚间后续）

28日已发布 **`DAILY_SIGNALS_V1`** 研究版本
`asof_20261028_41d83a1c2e34`，位于数据根目录的`research_versions/`。
默认`P0_COMPLETE`仍只读`versions/`；完整P0最新日期仍为27日。
研究版本的`quality_status=DAILY_SIGNALS_PASS_V1`、`engineering_only=true`、
`full_p0_pass=false`、`execution_qualified=false`，不能当作完整P0或模型接续输入。

真实入口及输出：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/run_postclose_daily.py --date 20261028 --signals-only
```

日报为`reports/postclose_daily/20261028/report.md`，结果
`TWO_STRATEGY_SIGNAL_RESEARCH_COMPLETE`：Deep **54** 个日线信号候选，RET120 **100** 个
Q5候选；候选CSV保留冻结信号记录和排序，不是模型待入场计划。模型进入、容量、持仓
接续、退出、权益与收益均未计算；后续模型父运行读取器拒绝该报告和研究账本。
相同输入版本、真实PIT和策略指纹再次运行会复用结果，不重复入账。

`price_tick`依赖按真实调用区分如下，冻结策略文件和容差不变：

| 直接位置 | 实际用途及缺失影响 |
|---|---|
| `p0_rq_input.real_observation/histories`、Deep `ret20_at_close`、RET120 `build_daily_snapshot` | 真实成交观察与信号特征，不读取tick；原观察数、覆盖和排序门槛保留 |
| 两模型的`entry_fillability/standard_open_v1` | 用`open >= up_stop - tick/2`判断涨停不可入场，确实需要适用tick，不能删除或改容差 |
| `p0_rq_input.execution_bar`及本地模型wrapper | 要求tick及当日证据；Deep busy、RET待入场/持仓与价格依据会被未知tick阻断 |
| 两模型退出及RET价格依据 | 数学退出规则读日历/有效收盘；现模型封装仍需执行证据，价格依据另外保存tick；本次不冒充已完成退出或结算 |
| Funnel `features_for`/结构分支 | 读取冻结OHLC/金额窗口；本次未资格化风险预览与退出，也没有恢复该策略完整运行 |
| `validate_day`及独立准入 | 原完整P0仍要求tick；新增研究资格只免除信号阶段不消费的缺失tick，不放行坏行情、非法已知tick或未知停牌 |

原5,223条28日日线、日期化停牌、身份及CSI500直接复用，`price_tick/tick_available_at`
保持NULL。现有两只证券的报价单位只有当日观察证据，没有开盘历史适用性证明，原件保留，
不贴入历史执行字段。本轮get_tick_size调用为0；经补充批准的同一认证、4次API仅取得
核心500只因子及2只真实行动缺项的原条款（分红、拆分、配股），Deep行动覆盖已通过。
原采集、成员和身份观察时间分别保留；benchmark绑定原HTTP响应完成时间18:04:32，
新封版时间独立记录，不冒充首次历史可用时间。

新增验证覆盖缺tick时同输入特征/Q5/仿射值一致（包括保留`2.328`）、执行仍拒未知或
不适用tick、两种发布资格隔离、失败staging保留旧版及实际日报入口幂等。未重跑旧套件。
源码前像及回执位于`reports/rqdata_tick_scope_20261028/`，数据和运行原件不入Git。
两个已有任务、时刻、截止及安全边界均未修改；完整模型成交/结算仍缺28日有效tick证据，
不以此次研究发布追认Forward或宣称29日任务成功。

## 当前部署快照（2026-10-28 晚间）

本节记录此次 Git checkpoint 对应的实际运行状态；下文保留早先隔离候选验收及其
当时的审批边界，不能把历史的“尚未部署”表述作为当前状态。

- RQData 是唯一日常外部数据源。生产入口使用已验证的 HTTPS License 认证、严格返回
  适配，以及 `all_instruments` 的 gzip / 正常 chunked 结束判断。证书、主机名、大小与
  超时限制保留；凭证从既有受控环境入口读取，Token 仅在内存，不回落原生 TCP。
- 完整身份实际读回为当日 5,223 条，正常采集按每天完整响应确定范围，不固定这个数量。
  完整原件和实际观察时间绑定后复用；未知身份、状态和冲突继续阻断。
- 两个 Windows 数据任务均已启用，既有下一触发为 **2026-10-29 08:20 / 16:10**。
  实际入口是 `.venv\Scripts\python.exe -X utf8 -B scripts\run_data_only_daily.py`
  加 `--phase preopen` 或 `--phase postclose`；已有窗口、截止和防重入规则保持。
- 已发布数据上的本地日报入口是
  `.venv\Scripts\python.exe -X utf8 -B scripts\run_postclose_daily.py --date YYYYMMDD`。
  输出位于 `reports/postclose_daily/`，保留 Deep / RET120 原排序、容量、结算和研究单位；
  原始信号、待入场计划和实际模型进入分开记录，真实账户保持未知。
- 最新实际发布仍为 **2026-10-27 / asof_20261027_5b3800ece8a8**。身份、发布和两策略
  日报入口已部署；不表示连续日更全面通过，也不表示两个任务已经在新部署后成功运行。
- 28 日补采已保存部分行情、停牌和 CSI500 原件，但全池 `get_tick_size` 在 READ_BODY
  超时，未封版或发布28日版本。补链未完成；历史研究继续 NOT_FORWARD，正式 Forward
  没有创建或推进。

直接部署及运行回执保留在本机 `reports/rqdata_chunked_eof_fix_20261028/DELIVERY.md`
和 `reports/rqdata_backfill_20261028/DELIVERY.md`；这些运行原件、数据库、日志和临时
诊断文件不进入本次源码提交。复用已有离线检查和真实运行证据，本次不重跑旧验收。

## 早先隔离验收记录

核验基线：`main` / `19c917805365a97f1d1dc8746dcee21b3f326588`。
本次工作仅处理本地代码、隔离发布副本与隔离研究输出。RQData 是当前唯一日常外部
数据源；不自动换源，不恢复 Full-QMT/Broker。`QMT_FRONT_AFFINE` 是冻结计算口径，
不表示触发 QMT 获取。

## 现场事实与证据边界

27 日股票数据已经取得并发布。现场版本是
`D:/positionx_data/market/p0_daily_inputs_v01/versions/asof_20261027_a4d668c81839/`，
数据库 SHA256 为
`a4d668c81839bafe3e9d92b13736b580292997ee38b34dbdd75f99c97ae68cf0`，
manifest SHA256 为
`8c7d412dd4a5f2afaa5a517e601cd3a2c0338aff1b648fbb82e9b599f6e8c675`。
`reports/postclose_stock_20261027_resume/manifest.json` 保存了 26、27 日各 5224 行、
各 7 条停牌/零成交记录及实际获取回执。不得沿用旧文档中的“27 日数据尚未取得”。

`D:/positionx_data/ops/three_strategy_data_v1/20261027_postclose.json` 是旧入口生成的
输入检查快照：Deep 500 个历史、500 个 ret20 有效；RET120 500 个有效、Q5 100；
Funnel 1036 个候选和结构特征、1036 个有限 ADV20。该入口没有运行两个策略的完整
信号/模型账本流程；Funnel 当时使用盘后成员参考，摘要仍标记
`HISTORY_ONLY_PENDING_TRUE_PREOPEN_PIT`。这份 GREEN 不能代替本次真实策略消费验收、
连续链或正式 Forward 结论。

27 日真实盘前 PIT 已存在：
`D:/positionx_data/pit_live/manifests/20261027T083550837725+0800_preopen.json`，
其成员文件 SHA256 为
`2bf383917ad6778497d04f909a244f53b05d831310fa821f4cca3984ecf28933`。
28 日另有 `reports/data_only_20261028/pit/manifests/20261028T082003+0800_preopen.json`；
它不能替代 27 日 PIT。每次运行均应重验对应日期、捕获窗口、数量、互斥性及内容哈希。

2026-10-28 用户补充路径后，已只读读取 `D:/数据来源探查.pdf`，共1页，SHA256为
`7cb51553072f369a1a890a1a2c57aac5a0d8a147f6afcccaa230c4715330884b`。
初次交付时尚未定位该文件；此前交付记录保留，补核记录位于本次
`pdf_baseline_followup/pdf_baseline_review.json`。PDF的六类来源说明与现场原始表及
本次读回未见实质矛盾；它提供来源基线，不替代P0、连续链或Forward验收。

| PDF来源基线 | 现场对照和保留边界 |
|---|---|
| 股票OHLC、量额、昨收及复用历史标记为RQData | 新封版raw_daily的1,382,491行均有RQ来源标识；未借此改标来源不明的指数历史 |
| 27日08:35真实盘前成员 | 原manifest实际捕获08:35:50.837725—08:35:51.394379，成员文件hash与最终研究绑定一致 |
| 当日身份、ST字段及停牌布尔；退市更新完整性未确定 | 保留Other36及原始退市零日期；不以名称猜ST，不推断复牌/退市事件 |
| 涨跌停来自RQ原始日线 | raw_daily实际保留limit_up/limit_down；没有以价格自行推算替代 |
| RQ历史公司行动加27日两只补数，front为计算口径 | 既有action_capture及captured_actions确认601866/601990的26日事件于27日18:54观测；不追认26日前可用，未新增取数 |
| 26/27日指数已取得但未并入原发布库 | 两日原值三方一致；已修复新TEMP候选，旧生产库保持原状；更早指数来源仍单独保留 |

PDF所述旧Full-QMT金额仅作为人民币单位交叉核对材料，与本次既有CNY金额证据相符，
不构成日常供给或恢复QMT；该PDF未用于推导依瓦数据已使用。此次补核还发现继承
元数据问题，已按下文追加最小修复；未修改旧策略产物或将原验收升级。

## 唯一入口、绑定与失败恢复

| 阶段 | 现有实现和唯一数据入口 | 本次候选要求 | 失败恢复位置 |
|---|---|---|---|
| PIT | `prepare_daily_data.preopen`；真实盘前 manifest 及成员 Parquet | 按精确 T 读取原证据，保留真实观测时间与 SHA；不事后补造 | 保留失败材料；错过窗口即阻断该日 PIT，等下一实际交易日 |
| 盘后获取 | `collect_postclose_candidate.run`；RQData stock daybar、identity/status、既有 reference 原始材料 | 本轮离线复用已有 26、27 日 capture，不重新查询已有指数 | capture manifest 与 `increment.duckdb`；完整性未过不封版 |
| 验收/封版 | `prepare_daily_data.merge_candidate` → `DailyInput` | 新隔离目录；数据库、metadata、manifest、COMPLETE 逐层绑定；COMPLETE 最后写 | 保留失败目录，使用新输出重试；旧完整版本不变 |
| 发布后读取 | `core.p0_rq_input.DailyInput` | 核验明确 manifest 和数据库 SHA；精确 T；指数来源与点位保留，不混标旧历史 | 未完成版本不可消费；原发布版继续保留 |
| Deep | `run_postclose_local_candidate.run_model` → 冻结 Deep 函数 | 同一输入版本、27 日 PIT、身份/状态与 front 契约；独立门槛 | 该策略 BLOCKED 保留精确错误，不伪造结果；其他策略独立执行 |
| RET120 | 同一候选的 `run_model` → 冻结 RET120 函数 | 同一输入版本与 PIT；保留 400/500、121 观测及冻结排序 | 同上；历史缺口不填零，不改收益口径 |
| Funnel | 冻结 prior → `qualify_postclose_funnel` 的结构/ADV20/Gate 函数 | 明确绑定实际 PIT 集合、同版 raw 行情及已验金额证据；退出仍独立未资格化 | prior、逐证券特征与阻断保存在同一新输出；不产生持仓或收益 |
| 结算/日报 | 现有隔离模型账本、`previous_run`、`plan_freeze`、`report.json/md` | 父产物哈希、相邻交易日、实际计划冻结时间；摘要分别报告各阶段状态 | 失败日保留；只接合法父运行，不跳日，不修改正式账本 |

数据发布成功与策略成功必须分别记录。任何策略输入不足、指数比较不可用或 Funnel
退出未资格化，均不得通过总状态掩盖。模型结果保持隔离研究/NOT_FORWARD 属性。
新建的隔离空研究起点也不是过去已事前冻结的交易计划；正式 Forward 另按实际冻结
时间、执行事件及有效授权判定。

## 最小生产应用范围

本次不执行下列生产动作。需要的范围限于：

1. 审阅本次发布修复及真实读回证据后，把已核验的 26、27 日增量发布为一个新的完整
   生产版本。保留旧版与历史来源标签，不覆盖旧版本或改写其 manifest。
2. 对日更任务显式绑定 `PIT → 盘后验收与发布 → 各策略独立计算 → 结算/日报`。
   复用现有入口及账本恢复机制，去除运行时盘后成员替代真实 PIT 的隐含路径。
3. 若批准将候选接入生产，单独审批 `run_data_only_daily.py` 的相关生产/调度语义、
   对应配置和实际任务入口切换。当前入口只做输入资格检查；不能因旧 GREEN 自动
   推进正式账本。`config/ops/p0_rq_input_v01.json` 的 `source_policy_note` 仍是历史
   Full-QMT primary / RQ transition 说明，应定点更新为 RQData 单源；不改 front/back_ratio
   契约、证据哈希或未知状态规则。Windows 任务安装、启停或触发仍须单独授权。
4. 正式模型账本起点、实际计划冻结及后续执行事件另行授权。不能把 27 日事后复算
   的结果回填为正式 Forward，也不能从数据发布批准推导交易批准。

失败恢复继续使用现有完整版本和新尝试目录，不删除报警、旧运行记录、锁或历史证据。
候选回退为停止使用本次源码增量与新输出；本次没有生产数据回滚、任务回滚或 Git
提交步骤。

## 本次实际结果

### benchmark 发布与读回

可运行的离线封版命令如下，输出必须为新目录。`seal` 不初始化供应商，装载现有
离线边界；合并使用事务，新版本 COMPLETE 最后写，后续验收/读回失败撤销本次标记。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/prepare_daily_data.py --phase seal --date 20261027 --stock-output reports/postclose_stock_20261027_resume --output reports/rqdata_closeout_20261028
```

首次生成 `reports/rqdata_closeout_20261028/TEMP/published/`，版本
`asof_20261027_0caa378ceaa9`，数据库 SHA256
`0caa378ceaa97e9421bf97c3fabbb4b470f39513cc9b77755a3b0246328f3e02`，manifest SHA256
`37c10334de8f2af58e38478537ef2b1c75d27c5513ce864d290b8e634cda7bc4`。
这是新隔离发布副本，没有调用生产 `publish` 或切换选择器。

| 表/消费入口 | 日期 | 身份 | close | 来源/单位 |
|---|---|---|---:|---|
| `benchmark` / `DailyInput.benchmark_history` | 2026-10-26 | 000905.XSHG / 000905.SH | 6712.7274 | RQDATA / INDEX_POINTS |
| 同上 | 2026-10-27 | 同上 | 6511.4183 | RQDATA / INDEX_POINTS |

`references.duckdb`、`increment.duckdb` 与新副本的两日身份、日期、OHLC、prev_close
逐字段相等。没有重新查询指数或换算点位。重复合并复用同一数据库 SHA；冲突拒绝、
事务失败回滚、失败不留可消费 COMPLETE、跨日增量保留逐日来源绑定均有直接测试。
更早来源不明的指数留在原库、原标签；若旧表存在未知历史，新加来源列保留 NULL，
重合键不自动改标 RQData。读回证据为 `publication.json` 和 `verification.json`。

### 身份、状态与缺项

- 新副本 26、27 日 raw_daily / execution_status 各 5224 行、各 7 条停牌；停牌真值表
  `suspension` 与完整布尔状态表配套保留，没有补造复牌时间。
- 当日 `identity_observed_today` 为 5224，累计 `security_identity` 为 5225。当前池读取
  修复为精确日身份表；累计独有 `000016.SZ` 没有混入当前池，也没有据此判定退市。
- 状态续采按逐证券、逐所需交易日判断完整，不能因已有某日一行就跳过缺失日。
  重试时同键状态/tick 冲突阻断，已有实际观测时间保留。
- 当日 special_type：Normal 5028、ST 83、StarST 77、Other 36。Other 不从名称判定 ST；
  核心 CSI500 的 500 只均为 Normal。全市场 Funnel 的未知分类单独列出，不填正常。
  已有 `D:/TEMP/positionx_rq_security_offline_20261027T155957` 及其引用的停止尝试
  没有27日 ST 布尔响应（ST RPC=0 / dated ST NOT_VERIFIED），无法据此解析 Other；
  这不否定之后16:44已经取得的实际行情。逐证券清单为本次 `identity_unknowns.csv`。
- 全部 5224 个当日 `de_listed_date` 是供应商原始 `0000-00-00`，无法推导实际退市时间。
  当日 `status` 原值 Active 也不替代独立停牌布尔证据。
- 26 日 status/tick 实际观测在 27 日下午，身份也是27日快照；没有新增26日真实身份
  快照或倒填可用时间，不能追认为26日同日状态/PIT或正式连续运行证据。

### 研究计算与验证

最终三策略结果、固定源码后的运行目录和直接测试见下方最终验收记录。本次研究从
空持仓、空旧计划开始，前一交易日仅为研究边界游标，不补造过去 session。
两本正式段账本仍在10月22日；未追改历史计划或推进正式 Forward。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/run_postclose_local_candidate.py --date 20261027 --input-manifest reports/rqdata_closeout_20261028/TEMP/published/manifest.json --pit-root D:/positionx_data/pit_live/manifests --fresh-research --amount-unit-binding reports/postclose_stock_20261027_resume/funnel_live/amount_unit_binding.json --output reports/rqdata_closeout_20261028/research_20261027_final2
```

实际最终输出为 `research_20261027_final2/report.json`、`report.md`、两个独立模型库及
Funnel 候选/特征。08:37:07—08:41:07 完成，239.984 秒；业务状态为
`PARTIAL_RESEARCH_CANDIDATE`，不是正式运行绿色状态。

| 策略 | 实际运行到达 | 尚未通过或未执行 |
|---|---|---|
| Deep | 真 PIT 的 CSI500 500只×120条；冻结 ret20 有效500，信号/计划67；实际读回27日指数6511.4183点 | 新空研究起点，持仓/退出0；未验收连续链或正式Forward |
| RET120 | 500个有效特征、Q5信号/计划100；实际读回同版指数6511.4183点 | 新空研究起点，持仓/退出0；碎股仅研究单位；未验收连续链或正式Forward |
| Funnel | 真 PIT 分层有效数300/300、500/500、997/1000、3380/3424；候选1036，结构和有限ADV20均1036，金额CNY证据通过 | Other36的状态资格仍BLOCKED；退出仍未资格化，不产生持仓/收益 |

Funnel Gate 为无入场区间1009、T收盘位于区间15、等待回调12。这是冻结结构条件输出，
不是交易或完整风险预览的批准。未知分类涉及本次7个候选：`000078.SZ`、`002731.SZ`、
`300068.SZ`、`300167.SZ`、`301139.SZ`、`603398.SH`、`603922.SH`；没有修改排序或将它们
改为正常。完整逐证券字段在本次 Funnel summary 和 `identity_unknowns.csv`。

`TEMP/research_final2_verification.json` 复核39个源文件、22个产物哈希，全部保持；
SDK/网络/子进程/越界写违规为空。计划CSV和模型库与实际冻结hash一致；两本隔离库
各只有1个研究session，正式 `forward_sessions` 各0。两个生产段在最终运行窗口前后
hash相同，游标仍10/22、正式session各0；不把该窗口哈希检查外推为更早历史审计。

真实 Forward-start 判断对两策略均为 `BLOCKED_FORWARD_START`：计划永久NOT_FORWARD、
内容为MODEL_RESEARCH_ONLY、实际冻结发生在28日而非27日信号日。未创建或推进正式
账本。开发中止目录及先前输出保留，本次交付仅以 `_final2` 为最终研究结果。

41项直接相关测试通过：benchmark发布8、采集8、当日身份4、既有prepare入口4、
新研究绑定/独立失败6、Funnel输入3、既有本地候选8。仅使用mock、隔离库和可正常
清理的临时测试数据，未重跑无关历史专项。修改后的diff检查通过；未暂存、提交、
推送或改变Git配置。初次精确diff为 `source_changes.patch`；包含本次PDF文档补核的
更新diff为 `pdf_baseline_followup/source_changes.patch`，原交付文件保留。

接入生产前还须核定完整链时限：本次计算本身约4分钟，不含未来采集和发布；旧任务
安装脚本的5分钟总限时不能直接视为已验收的完整日更预算。此项与上节入口依赖和
配置修改共同属于待明确批准的最小应用范围。本次没有执行安装、切换或正式账本推进。

## PDF补核后的发布元数据修复

对照时发现首次TEMP副本继承23日父版的计数、时点及更早父哈希；原文件和首次
交付清单保持原状。追加仅修改 `prepare_daily_data.merge_candidate` 的版本元数据
计算，并在 `test_rq_benchmark_publish.py` 新增1项计数/时点/父版本保留回归。

新副本为 `pdf_baseline_followup/publication_candidate/TEMP/published/`，版本
`asof_20261027_5b3800ece8a8`，manifest SHA256为
`9b1bad3f1319f31331db3ba204ec165eccb2a95aa92f65f74efe490d9f16cad9`。

| 本版字段 | 新副本实际值/语义 |
|---|---|
| counts.raw_rows / symbols | 1382491 / 5225，直接查询新库 |
| counts.appended_rows / business_date_symbols | 10448（26/27两分区合计）/ 5224（精确27日） |
| ingested_at / available_at / availability | 同一次真实封版时间2026-10-28T08:47:05.434626+08:00，仅表示本地新版本组装/可用时间 |
| base_manifest_sha256 | 直接23日父manifest，51610f10ba1bc10f36d87fc0170dc8b57dd287de492e005d5f8eb46174be9c01 |
| base_version_metadata | 保留父版身份、原计数、三个原时间及更早父哈希；既有21日历史provenance原样保留 |

供应商完成回执仍为27日16:44:27.551118，身份观测上界仍为27日16:44:27.617629；
没有用28日封版时间替换它们。新副本的行情、身份、状态、停牌、因子、指数及已捕获
行动逐字段与首次副本相等；corporate_actions条款和available_at也相等，仅517行
observed_at反映本次新的真实本地读取时间。没有改写旧库或回填历史可用时点。

新增回归及本次9项发布测试通过；累计42项不同的直接测试通过。本次没有重跑三策略，
原 `_final2` 结果仍明确绑定首次 `0caa378ceaa9` 版本，未改绑到新哈希；39个源文件及
22个原策略产物hash再次核对保持，生产版hash也保持。新副本另完成股票验收和指数
真实读取，详细对照见 `pdf_baseline_followup/pdf_baseline_review.json`。

包含PDF补核和元数据修复的当前diff及交付清单位于 `pdf_baseline_followup/`；初次
`delivery.json`、diff、策略冻结与验证文件保留，未追改其生成时的事实。生产应用范围
仍为前述独立审批项，优先审阅本次元数据已修正的新候选。
