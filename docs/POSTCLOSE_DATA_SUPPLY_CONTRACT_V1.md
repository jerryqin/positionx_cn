# Postclose Local V1 — 每日供给与真实前向准入合同

本次基线：`main` / `e54a29b0670b8018a3b686f637479a8a97c9a023` 加既有工作区；目标交易日为2026-10-26。来源、封版和入库代码只读检查，未启动SDK或新查询。直接证据在 `reports/postclose_data_admission_20261026_verified/TEMP/data_gates.json`。

**实查结论：消费和部分封版可以离线，但目前没有已证实、获准且可持续的每日新增供给。** 原RQ接口代码存在，不代表今后可用或本轮获准调用；原QMT获取入口不恢复。26日四个数据门槛均为BLOCKED，正式模型/Forward账本未创建或推进。23日事后计划和26日合成样本继续永久NOT_FORWARD，不重跑已接受41项测试或合成结算。

## 1. 每日实际来源和更新链路

| 数据项 | 当前可信资产及最后日期 | 实际更新入口与依赖 | 离线可复用部分、持续性和阻断 |
|---|---|---|---|
| 股票raw日线、prev_close、上下限、量额、成交观测 | `D:/positionx_data/market/p0_daily_inputs_v01/versions/asof_20261023_e2074bcd5104/`，manifest/COMPLETE及数据库绑定通过；最后合格日23日，当日5224行/5224身份，5条停牌/零成交记录，未当作成交 | `scripts/collect_p0_rq_close_v01.py:main → init_provider → collect`，调用RQ `is_data_ready`前后两次、`all_instruments`、`get_price`、`is_suspended`、`get_tick_size`、`get_ex_factor`，必要时实际公司行动条款。这个获取分支依赖rqdatac，不能当成纯离线入口；本轮未调用，未来可用性未核实 | 已封版历史可离线读。`seal_capture`可从真实已保存材料封版，但要求前后完整性回执、相邻交易日和质量检查；当前没有26日材料。继续更新需要独立确认供给通道及授权，不能改日期复用23日 |
| 身份、上市/退市、ST分类、最小整手、证券状态与tick | 同一23日封版库 `identity_observed_today` / `execution_status`；历史`security_identity`保留5225个曾有身份，不能把累计身份数等同当日5224个有效源池 | RQ每日获取链同上。盘后 `all_instruments(date=T)` 与日级状态的捕获时点保留在材料中 | 最后可复用23日盘后身份和状态。它不是26日状态，也不是盘前成员证明。未知状态不能填false/0；历史退市身份不删除，也不补退市后的行情 |
| 真实盘前PIT股票池 | `D:/positionx_data/pit_live/manifests/20261023T082006+0800_preopen.json`，23日08:20:06—08:21:11真实采集，300/500/1000且互斥；最后合格23日 | `scripts/daily_pit_intake_v01.py:capture_qmt_membership` 名称保留历史，但RQ分支实际为 `rqdatac.init → core.p0_rq_input.membership_frame → index_components(return_create_tm=True)`；另一分支依赖QMT。总 `run(preopen)` 还访问CNINFO并写研究库，不能作为本轮只读命令执行 | 只读验证/消费完全离线。未来必须按真实当日窗口取得成员、身份及证券状态，记录provider创建时间与本机实际观测时间；26日没有现成真实同日PIT，盘后不能补造。现有总任务的外部副作用不能被“PIT”名称豁免 |
| CSI500指数日线 | `data/research/positionx_research.duckdb:foundation_benchmark_index_daily`，绑定EVA CSV，1633条CSI500日线至2026-09-24；另有独立的旧 `daily_bars` front片段至10-15、back_ratio片段至10-13 | 没有已接通的独立日更入口。旧 `core.p0_rq_input.strategy_histories` 对指数调用 `get_xtdata → XtDataWatchlistAdapter.history`，即使称本地读也需真实QMT运行时；股票RQ collector只采CS股票，不会自动获取指数 | 各历史序列可分别复用，不自动拼接/换复权。缺26日已验收指数及新供给绑定；23日基准锚点若要做连续收益也需精确补齐。指数点位不能被股票、成分股均值或研究market_median_return替代；指数缺口不阻断其他输入齐备的Deep/RET120 |
| 原Funnel结构目录 | `D:/positionx_data/catalog/market_data.duckdb:market.daily_bars_v1` 最后10-15；基础+增量UNION ALL | `scripts/update_a_share_daily_v1.py:main → get_xtdata → resolve_plan/process → write_session → update_catalog/commit_state`，原获取与日历依赖QMT。旧orchestrator还将其置于真实账户Preclose依赖后 | 本轮不执行任何run/check或改调度。已有日线可作暖机，不代表新目标日已到；更新会写生产market资产和catalog，需另行授权，不从当前只读/候选工作推导 |
| 额外发现的现有Funnel离线行情材料 | `D:/TEMP/positionx_qmt_primary_replacement_20261025/run_20261025T162032_65afdb6e/replay/qmt_market.duckdb`，1354129行，到10-23；本次核对原SHA清单，价格raw/none，amount=CNY，volume按手保留 | 这是一份25日留下的真实采集/隔离计算材料，不是新启动的供给。旧采集依赖QMT，文件读取不依赖SDK；没有本轮运行其程序 | 对已接受23日1036候选，全有65条历史，1035只有精确23日行情；301139.SZ只有到08-28的135条。单位证据为77行/10证券的有限核对，不能外推全量逐点验真。旧条件成员、事后观测、无首次可用时点证明仍保留；不升格为PIT、持续供给或Forward，不替换生产目录 |

离线RQ消费入口为 `core/p0_rq_input.DailyInput`；只验证封版资产，不主动下载。`collect_p0_rq_close_v01.seal_capture` 复制上一版并追加到独立新库，生成quality/metadata/manifest，COMPLETE最后写；`publish`再将完整版本复制到固定生产资产根，COMPLETE最后复制。**本轮没有执行seal/publish或生产入库**；可复用其合同及校验，不默认批准仓库外写入。

## 2. 最小字段和质量验收合同

| 门槛 | 最小字段与绑定 | 必须通过的质量/时点要求 |
|---|---|---|
| STOCK_DATA_READY | symbol/order_book_id、date、raw OHLC、prev_close、volume、amount/total_turnover、num_trades或等价真实成交证明、真实limit_up/down或显式无价格限制证明；精确日期的status_suspended、status_available_at、price_tick、tick_available_at；身份name、上市/退市有效日、ST/special_type、board及最小整手 | 日期精确T；当前源池身份、行情、状态一一对应，重复/缺键/非有限/价格关系错误阻断。停牌与有成交冲突阻断，停牌/零成交不制造成交。状态必须明确布尔，tick与状态时点同日且不晚于决策。整手必须正整数。元/股、量的原始单位、人民币金额来源明确记录；不擅自转股/手。raw完整不代表每只股票特征有效，prev_close等无效的历史窗口仍按冻结策略阻断/invalid |
| 股票封版完整性 | 来源、交易日、实际availability/ingested_at、版本号、文件清单与SHA、数据库SHA、COMPLETE、供应商完整日数据回执 | 同日盘后完整日数据；现有门槛15:30之后、前后完整性回执PASS、相邻交易日新增，无已有/未来重复行。保存失败材料，不覆盖旧版，不靠改期或改hash变绿。新来源不可伪装成RQDATA，需独立身份映射与口径验收 |
| PIT_READY | 同日snapshot_date/effective_date、index_code/symbol、source/version、provider_created_at、actual observed_at、capture开始/结束、内容SHA；当日全市场身份及状态要有独立实际时点记录 | 冻结消费者按盘前09:20前完成检查，300/500/1000准确且互斥；provider_created_at≤observed_at，观测落在本次实际捕获区间且同日。当前成员不能倒推历史、盘后身份不能冒充盘前成员。四个READY中的PIT标志校验盘前成员；身份/状态同时由STOCK标志校验，核心必须两者齐备 |
| Deep输入兼容 | 当前CSI500每只120条日线、raw执行价/状态、最小整手；仿射front所需现金/送转/配股条款、事件可用时点、同日覆盖范围和已接受证据hash | 继续 `QMT_FRONT_AFFINE` 有限范围合同；不能以RQ_PRE直接别名代替。新入池标的无受支持行动覆盖或历史不足则阻断。Deep校验失败不会伪装成RET120失败，两者独立报告 |
| RET120输入兼容 | 当前CSI500500只，至少400只具有精确T和121个合格真实观测的冻结close/prev_close特征；raw执行价、状态、上下限、tick与研究因子分开留证 | 保留原400/500与冻结排序/五分位，不把缺历史填零；保留坏因子窗口阻断、真实成交观测与原始价/研究单位区别，不以RQ_POST代替冻结收益口径 |
| BENCHMARK_READY | 000905.SH原生指数序列的精确T close（完整日线可保留OHLC）；来源、点位单位、版本和可用时点/完整性绑定；连续比较另需精确基准锚点 | 原生指数口径固定，精确日期，正值、唯一键、已接受来源绑定；不得拿邻近日、ETF、成分股均值代替。不同历史来源/标签不自动拼接。指数量额单位不明时不用于流动性判断。该门槛仅控制基准比较 |
| FUNNEL_STRUCTURE_READY | 实际T的冻结prior候选池与内容hash；260自然日窗口内raw OHLC、pre_close、amount、保留原单位的volume；结构至少65条日线、最后20条金额用于ADV20；目标日名称/板块信息 | 保留冻结累计close/pre_close后映回raw价，逐证券精确T与暖机检查，不用目录全局max掩盖缺证券。65行不是可成交证明；金额必须与人民币NAV/风险预算口径一致。变量volume_ratio实际使用amount，volume不得静默按股/手互换。旧23日候选不能当26日候选。退出尚未资格化，不生成Funnel持仓/收益 |

核心许可为 `STOCK_DATA_READY && PIT_READY && 各自冻结输入兼容/覆盖通过`。单纯BENCHMARK缺失或Funnel缺口不阻断核心计算。四个READY与信号PASS、Forward候选准入、正式模型账本推进是不同状态；数据PASS本身不推进账本。

## 3. 当前直接验证与可复用数据

最终版本的目标实查输出（早期开发回执保留并标为SUPERSEDED，非等候数据的轮询）：

| 2026-10-26门槛 | 结果 | 直接阻断 |
|---|---|---|
| STOCK_DATA_READY | false / BLOCKED | 无精确26日封版；最后合格23日5224行，原始字段/状态检查PASS；front/back_ratio现有兼容证据hash通过 |
| PIT_READY | false / BLOCKED | 无26日真实同日盘前manifest；23日08:20真实捕获只作23日历史证据 |
| BENCHMARK_READY | false / BLOCKED | 注册EVA序列精确26日行数0，旧观测片段未覆盖目标；无已接受新序列绑定 |
| FUNNEL_STRUCTURE_READY | false / BLOCKED | 原目录和旧TEMP材料均无26日原始行情、26日实际候选池与新的供给证明 |

`data_gates.json` 同时记录原目录/旧TEMP的区别和可复用范围，并验证相关源文件在本次检查后的hash一致。当前可直接离线用的是：23日已封版RQ股票、23日真实PIT、分别保留的历史指数、已有暖机行情、有限单位核对材料和原冻结策略。没有将这些历史材料选为26日输入，也没有重跑23日信号、26日合成结算或41项测试。

## 4. 需要新增供给的精确清单与独立申请

独立申请号 `POSTCLOSE_DAILY_SUPPLY_V1`，状态 **PENDING_SEPARATE_AUTHORIZATION**。可优先由一个现有授权来源或离线文件交付满足下列内容，不进行供应商大型评选，也不直接调用原RQ、QMT或新服务：

1. 从首个可真实捕获的交易日起，在盘前09:20前交付当日300/500/1000成员及有效期、实际观察/供应商创建时间；同时交付当日全市场身份和证券状态。26日PIT已错过且没有现成材料，不申请事后补造。
2. 每交易日完整收盘后交付该日股票raw十字段、真实状态/tick/身份、完整性回执和可用时点。既有历史可复用，不重买全历史；只有新入池/缺历史证券才补其具体不足。公司行动条款/因子与冻结复权兼容证明必须可持续更新。
3. 交付CSI500原生指数精确日线及序列/点位/完整性证据；若要比较23→26收益，额外最小锚点为23日、26日close，不必为核心模型重启交易接入。
4. Funnel可先复用现有真实暖机材料；每个新T候选需要精确T raw价格、pre_close与人民币amount。对301139.SZ这类缺精确T材料的情况，先核实真实交易/停牌状态，不能填造行情；其他不足仅补其窗口缺项。显式绑定来源、金额单位和原始价格，不自动混入RQ total_turnover。

交付应为独立TEMP中的DuckDB/Parquet及manifest/quality/时点/单位回执；不含账户、订单、凭证。该申请不包含安装SDK、生产发布、Scheduler或新外部查询的批准。原RQ是否可在既有授权下持续交付需单独确认；若不可用，再对满足同一最小合同的一个替代交付通道申请授权，**不会从“已有封版RQ”推出“RQ未来仍可用”**。

## 5. 第一个真正Forward日期的准入

现在的 `first_forward_signal_date = null`，没有创建、复制或推进正式账本。27日仅是日历上的后续最早可能日，不是“27日必然Forward”。实际起点是第一个同时满足下列条件的未来交易日T：

- T的真实盘前PIT和身份/状态已在实际窗口记录；T完整股票数据已验收，当前策略的冻结输入兼容、暖机和覆盖通过。缺基准仅令基准比较不可用；Funnel独立。
- 计划确实由该合格T输入与冻结策略生成。生成时间不得早于数据实际可用时间，冻结时间须现场记录在T；全部早于批准交易日历给出的下一交易日事件，不能在事件已过后追认。
- 实际计划文件SHA、输入版本/manifest SHA、策略指纹（RET120含既有冻结证据绑定）、T、下一交易日和事件时间均可核验；计划文件变动、版本漂移、时区/时序不明一律阻断。
- 23日事后计划、26日合成样本及任何NOT_FORWARD/REPLAY来源永久不合格；不能改标记或复用旧持仓来“接续成Forward”。真正前向初始模型持仓/成交必须为空，不继承回放/样本账本。

`forward_start_decision` 是可运行的准入判断，数据齐备也不自动代表正式Forward验收。当前生成 `TEMP/forward_start.json` 和 `TEMP/forward_seed_contract.json`，动作明确为NOT_CREATED_OR_ADVANCED；没有生成新的Forward DuckDB。待真实合格数据与实际冻结计划存在时，再依此门槛决定独立新起点，而不是恢复23日旧计划或回填26日PIT。

## 6. 本轮最小候选、运行与验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/postclose_data_admission_v1.py --date 20261026 --output reports/postclose_data_admission_20261026_review
```

目录必须不存在。该候选只读已有资产和代码，复用SDK/网络/子进程/越界Python写入拦截；DuckDB只读连接且关闭扩展自动安装/加载。只在仓库内新输出目录写决定与证据，没有数据获取、封版、发布或模型计算命令。

本轮仅新增：`scripts/postclose_data_admission_v1.py`、`tests/test_postclose_data_admission_v1.py`、本文及新TEMP结果。10项**新数据合同/准入**测试通过：精确日期不回退、未知状态/冲突交易、原单位、旧状态时点、整数整手、可选基准/Funnel不连带阻断、版本/文件hash/时序、NOT_FORWARD和持仓继承拒绝。没有重跑旧41项或任何合成交易结算。

回退为弃用新增候选与新输出；旧候选、历史证据、冻结资产、生产数据库、Scheduler及Git暂存/提交不变。全仓原有配置与ops_runtime的CRLF行尾差异继续保留，本轮新增文本另作完整内容与语法检查。
