# Postclose Local V1 — 2026-10-26 本地核验

核验基线为 `main` / `e54a29b0670b8018a3b686f637479a8a97c9a023` 加当前工作区。继续接受 2026-10-23 的 `PARTIAL_RESEARCH_CANDIDATE`，没有追认 Forward。以下结论是本次带时间戳的本地检查，具体哈希、字段与逐证券结果在新产物中。

## 两个数据缺口及复用范围

| 输出 | 精确日期、字段 | 已有资产与可复用范围 | 口径与具体缺项 |
|---|---|---|---|
| CSI500 基准比较 | 最小需要 `000905.SH` 的 2026-10-23、2026-10-26 精确 `close`，加日期、身份和来源绑定；历史日线可保留 OHLC、volume、amount | `data/research/positionx_research.duckdb` 中 `foundation_benchmark_index_daily` 指向 `D:/positionx_data/incoming/eva/20260925_foundation_assets/data/research/px_assets/benchmark_index_daily.csv`；CSI500 有1633行，2020-01-02至2026-09-24。CSV哈希 `ff432d62fa73295ae92cc7162c8de3a4273357a9904a39ad087f1f6e48b4bf73` 与 READY 注册记录一致 | 指数价格单位为点位，不是股票元/股。基金/个股、成分股均值和研究 market_median_return 不能冒充指数。CSV中 volume/amount 没有明确单位证据，不能用于流动性和成交判定；不影响已核对的历史close研究复用。没有23/26日数据 |
| CSI500 已观测片段 | 独立保留各序列实际日期 | 同一研究库 `daily_bars`：front / MINIQMT_FORWARD_OBSERVED 为9行，2026-09-28至10-15；back_ratio / MINIQMT_RET120_FORWARD_OBSERVED 为4行，10-08至10-13。`forward_sessions.benchmark_close` 只有对应日期收盘值 | 不自动拼接两个复权标签或 EVA 序列，不把稀疏观测称为完整连续历史。若要从10-15连续更新，缺10-16、19、20、21、22、23、26；仅做23→26基准收益，最小补充为两个精确close及口径绑定 |
| Funnel 结构与风险预览 | `market.daily_bars_v1` 的 symbol/date、原始 OHLC、pre_close、amount、volume；23日窗口起点2026-02-05，26日起点2026-02-08；冻结代码读取260自然日窗口，结构至少65条按原口径留存的日线，ADV20用最后20条日线金额；行数不等于可成交证明 | `D:/positionx_data/catalog/market_data.duckdb` 的原视图由基础与增量视图 UNION ALL，至2026-10-15。对已接受23日的1036个候选：目标日行0，窗口有历史964个、至少65行963个；72个完全缺历史，另1个不足65行。详细名单见 `funnel_symbol_gaps.csv` | 价格保持 raw/none，结构使用冻结累计 close/pre_close，再映回原始价；原始volume保持供应商单位，结构引擎实际用amount，不以volume替代金额。风险预算NAV与amount需要一致的人民币口径。缺目标日和不足暖机观测，不能以全局max日期或邻近日替代 |
| 已封版股票原始数据 | 现有 raw_daily 含 OHLC、prev_close、total_turnover、volume、num_trades、limit_up/down；execution_status保留状态与tick时点 | `D:/positionx_data/market/p0_daily_inputs_v01/versions/asof_20261023_e2074bcd5104/daily.duckdb`，5225只股票，2025-09-15至2026-10-23；manifest/COMPLETE/兼容证据通过 | Deep 为已验收 `QMT_FRONT_AFFINE`；RET120 为冻结 `RET120_FROZEN_CLOSE_PRE_CLOSE`，执行价另保留raw。volume是 `RQ_VENDOR_ORIGINAL_NOT_CONVERTED`，不换算。现有两个策略契约没有自动授予 Funnel 的 `total_turnover→amount` 口径替换；可作为本地核对材料，明确金额单位、来源及raw价格绑定后才能替换，未静默混源 |

RQ foundation 库检出的指数表是成分股/PIT/权重，不是指数价格；其他 `market_benchmarks` 研究文件是横截面市场中位收益，不是 CSI500 行情。股票封版库中 `000905.XSHG` 行数为0，原 market 目录中的 `000905.SH` 行数也为0。

**最小补充范围**：指数两个精确close及来源/序列口径；Funnel每个实际候选的目标日原始字段、足够65观测的暖机段，以及金额/价格来源绑定。对原目录中已具暖机的候选可复用历史，只补必要日期；对72个无历史和1个短历史候选补其具体不足。无需重新下载全市场历史，更不涉及SDK选型。

## 跨交易日接续

候选入口新增显式 `--previous-run`。父运行的 report、两个复制账本及计划CSV必须与原产物清单哈希一致，父日期和两账本游标必须一致，再由批准交易日历验证只接下一交易日。复制后才进行事务计算；源账本不写。重复日、跳过缺日、被改计划或缺失待处理证券行情都阻断。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/run_postclose_local_candidate.py --date 20261026 --previous-run reports/postclose_local_20261023_20261026_verified --output reports/postclose_local_20261026_new
```

本次**没有10月26日合格封版数据或实际盘前PIT**，没有按实行情结算26日，也没有等候或轮询。正常接续仍需目标日合格股票输入；生成新的T-close信号还需真实同日PIT。本轮只在 `reports/postclose_local_v1_20261026_final/TEMP` 完成明确标记的合成样本。样本只结算旧计划，不制造26日信号或PIT；样本输出、账本状态和价基记录均带 `SYNTHETIC_SAMPLE_NOT_MARKET` / NOT_FORWARD 标记，不能作为正常父运行或业绩数据使用。

样本假定所有原始开收盘均为10，RET120研究因子0.37。结果：

| 样本 | 原计划 | 样本入场持仓 | 无槽位 | 入场名义金额 | BASE20单边成本 | 样本现金 | 样本退出 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Deep | 37 | 37 | 0 | 296000 | 296 | 703704 | 0 |
| RET120 | 100 | 30 | 70 | 300000 | 300 | 699700 | 0 |

Deep 每笔800股，保持最小整手向下取整；RET120按原symbol排序占用30个槽，保持碎股研究单位。BASE20/SHADOW50分别保留20/50bp往返成本，每边一半；两本现金均非负。30%是冻结定义的入场目标名义金额/开盘权益上限，费用导致RET120期末gross略高于30%，不是擅改入场限制，也未为强行压回30%改规则。首个S1没有到期退出，S20、S120以及停牌延期继续由原离线假数据回归验证。

两样本游标由23日推进至26日，同日重入与跳至28日被拒绝。对缺行情的样本，新复制账本仍保留原游标；原账本和原计划哈希不变。单只停牌不再使Deep全部待入场计划无法验证：该证券按原填单规则阻断，其他证券继续；应到期退出若缺真实有效close则阻断，未虚构退出价。

新计划生成实际时间与CSV/账本哈希保存在 `plan_freeze.json`。`require_plan_before_event` 验证冻结早于执行事件且计划哈希未变。该时间门槛不会把 NOT_FORWARD 升级为 Forward；当前入口无正式Forward模式。原23日计划于26日开盘后生成，时序门槛明确拒绝追认。

## Funnel 独立性及日报

Funnel 当前仍只有已接受的 prior：动量360、均值回归676。新增依赖检查按每只候选的精确日期、字段和65观测进行，不再把目录全局max日期当作全部候选就绪。来源仍是原目录，未新增外部查询或隐式RQ回退。退出未资格化，不生成Funnel持仓、收益或组合份额；这些缺口不阻断核心模型的独立验证。

旧报告 `reports/postclose_local_20261023_20261026_verified/report.md` 的 SHA256 为 `720888c9c02b2c419cbb193bab0c44491dc1d50856e9e076db608d62ce66fa1d`。第52—59行已经含字节0x3f问号，原因是上轮PowerShell stdin管道的中文替换，不是文件UTF-8解码失败。新报告由UTF-8源码中的完整模板生成，显式按UTF-8写入；原文件和原 verification 保留，不覆盖、不更新旧哈希。

新版报告在 `reports/postclose_local_v1_20261026_final/report.md`，明确分开23日模型计划/持仓/收益、26日合成样本、真实账户UNKNOWN、具体数据缺项与NOT_FORWARD状态。主报告旁有结构化 `data_gaps.json`、逐证券CSV、`TEMP/settlement_samples.json` 和样本复制账本。早期本轮报告仅因日线行数措辞修正而保留并标为 SUPERSEDED，不作为最终交付。

## 本轮范围与验证

- 修改 `scripts/run_postclose_local_candidate.py`：显式父运行接续、哈希/游标校验、计划冻结记录、逐证券Funnel依赖检查，以及明确隔离的合成结算验证路径。
- 新增 `scripts/validate_postclose_local_v1.py`：一次本地资产检查、原计划合成结算与完整UTF-8新版日报。
- 新增 `tests/test_postclose_local_v1.py`：9项接续、异常、规则和报告测试；加原候选8项、RET120 11项、Deep 8项、Funnel 5项，合计本轮41项通过。
- 新增本文；新增本轮报告与独立TEMP产物。

没有修改两个冻结策略计算模块、冻结配置、生产数据库、Scheduler、旧报告、旧验收或Git状态；工作区进入本轮前已有修改继续保留。回退只需停止使用本轮增量和新输出；没有生产回滚步骤。未提交或暂存。
