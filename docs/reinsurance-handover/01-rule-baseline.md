# 01 · 引擎规则基线

> rule_version `R-809d7c1.1` · commit `809d7c19bcdcac08332a970f2bc809707e532441`
> 本文所有数字可用 `python3 tools/check_samples.py` 复现。

## 1. 一句话概括

引擎把**全部损失行按文件顺序累加成一条 running cumulative**，再对每个 treaty 求"该层在本次 movement 前后被覆盖的额度之差"。

它不是保单级、赔案级或年度累计的分层计算，而是**一条单一累计曲线沿层切割**。

## 2. 调用链（全部真实路径）

```
cli.main                                    cli.py:6-11
 -> pipeline.run                            pipeline.py:5-7
     -> parser.load_records                 parser.py:1-5
     -> engine.calculate                    engine.py:1-10
     -> report.build_result                 report.py:3-5
```

`models.PipelineResult`（models.py:4-7）是一个冻结的二元组 `(project, data)`，只做信封，不参与计算。

## 3. 唯一的计算公式

对每一行 loss，按文件顺序：

```
start      = cumulative                       # engine.py:4 累加前
cumulative = cumulative + float(amount)       # engine.py:4
```

对每个 treaty（按 attachment 升序，engine.py:2）：

```
top           = attachment + limit            # limit 是层宽，不是层顶
covered_before = max(0, min(start,      top) - attachment)     # engine.py:6
covered_after  = max(0, min(cumulative, top) - attachment)     # engine.py:7
allocated      = round(covered_after - covered_before, 2)      # engine.py:8
if allocated != 0: emit row                                    # engine.py:9
```

收尾：

```
unallocated = max(0, cumulative - sum(allocated))              # engine.py:10
```

## 4. 逐行对照：F01-cross-layer-trace

输入：`C1/M1 = 120`、`C1/M2 = 80`；层 `L1 = 100 xs 50`（区间 [100,150]）、`L2 = 150 xs 100`（区间 [150,250]）。

| 行 | start | cumulative | 层 | covered_before | covered_after | 差值 | 输出 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| M1 | 0 | 120 | L1 [100,150] | 0 | 20 | 20 | **20 → L1** |
| M1 | 0 | 120 | L2 [150,250] | 0 | 0 | 0 | 抑制（engine.py:9） |
| M2 | 120 | 200 | L1 [100,150] | 20 | 50 | 30 | **30 → L1** |
| M2 | 120 | 200 | L2 [150,250] | 0 | 50 | 50 | **50 → L2** |

结果：`cumulative_loss = 200.0`、`unallocated = 100.0`、3 条 allocation。

复现：

```bash
python3 tools/trace_allocation.py samples/F01-cross-layer-trace.json
```

该工具会独立重算上述每一步，打印 `covered_before/covered_after` 并逐条比对引擎输出；不一致会以 `MISMATCH` 退出非零。

## 5. 已证明的规则边界（PROVEN）

| # | 边界 | 样例 | 源码 |
| --- | --- | --- | --- |
| P1 | cumulative 跨赔案共享，**不按 claim 重置** | F02 | engine.py:4 |
| P2 | 分摊只由**文件行序**决定；movement 名称不参与排序 | F03a/F03b | engine.py:3-4 |
| P3 | 层重叠会**重复计算**，不净额化 | F04 | engine.py:6-7 |
| P4 | 层间缺口落入 unallocated，与"超出最高层"不可区分 | F05 | engine.py:10 |
| P5 | `unallocated` 被下截断为 0，**无法表示超配** | F06 | engine.py:10 |
| P6 | `currency` 从不被读取，多币种直接相加 | F07 | 零命中 |
| P7 | `round(x, 2)` 为 **half-to-even**，非 half-up | F08a/F08b | engine.py:8 |
| P8 | 小于半分钱的 movement 不产生任何 allocation 行 | F09 | engine.py:8-9 |
| P9 | 无文件身份/去重，重复行被计两次 | F10 | parser.py:5 |
| P10 | 同一 movement 在"全历史"与"当月增量"文件中分摊不同 | F11a/F11b | engine.py:4 |
| P11 | 摊回降低 cumulative，**免费释放层容量**（无恢复款） | F12 | engine.py:4 |
| P12 | 数值字符串被静默强转 | F13 | engine.py:4 |
| P13 | attachment 相同的层按输入顺序决定先后（稳定排序） | F14 | engine.py:2 |
| P14 | treaty 变更作用于整次运行，**无法按生效日拆分** | F15 | engine.py:2 |
| P15 | 零金额 movement 不产生行，"无行"与"未评估"不可区分 | F16 | engine.py:9 |
| P16 | 逐 movement 舍入**不满足望远镜求和**（各段舍入后之和 ≠ 总宽舍入），漂移被 unallocated 掩盖 | F17 | engine.py:8,10 |
| P17 | 非对象行被静默丢弃，无计数、无告警 | F18 | parser.py:5 |

## 6. 形状契约（稳定部分 vs 易变部分）

**稳定**（下游可依赖）：
- 顶层键固定为 `project / status / cumulative_loss / allocations / unallocated`（report.py:5）。
- `status` 恒为字符串 `"ok"`，**即使业务上可疑**（F06 超配仍返回 ok）。
- 每条 allocation 的键固定为 `claim / movement / treaty / allocated`。

**未定义 / 易变**（下游不得依赖）：
- `allocations` 的**顺序**无契约；它由 loss × treaty 的双重循环自然产生。
- 无 `currency`、无版本、无源行号、无期间。
- 无空值/零值行的显式表示（零被过滤）。

## 7. 关键认知

> 引擎只有一个数字状态：`cumulative`。所有"版本""币种""期间""重复"的问题，最终都表现为**这个单一累加器被谁、以什么顺序、按什么符号喂入**。任何超出此模型的业务诉求，都不是配置问题，而是需要改代码的缺口。

下一节：`02-field-lineage.md` 说明究竟哪些列喂进了这个累加器。
