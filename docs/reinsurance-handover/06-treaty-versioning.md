# 06 · Treaty 版本与生效

> rule_version `R-809d7c1.1` · 登记册 `registers/treaty_change_register.json` · 样例 F15、F01

## 1. 现状：引擎没有"版本"这一层抽象

`engine.py:2` 只做一件事：读一个扁平的 `treaties` 数组，按 `attachment` 排序。数组里只有 `name / attachment / limit` 三个字段。

因此：

- **没有** `version`、`effective_from`、`effective_to`、`endorsement` 字段。
- **没有**机制让某条 movement 选用某一版 treaty。
- 一次运行 = 一个数组 = 对**全部** movement 生效。

> 结论先行：**当前实现无法表示"treaty 在期间中途变更、且只影响变更后的 movement"。** 这是模型缺口，不是配置项。

## 2. 登记册如何补位（在文件层，而非引擎层）

由于引擎不版本化，版本纪律由 `registers/treaty_change_register.json` 承担：

```json
{
  "treaty_id": "T-L1",
  "version": "v1",
  "layers": [{"name": "L1", "attachment": 100, "limit": 50}],
  "engine_representable": true
}
```

- `effective_from/to` 被显式写成 `"not-representable"`，而不是编一个假日期。
- 每个版本记录 `engine_representable`：v1 为 `true`（能跑），v2 为 `false`（能跑但语义错误）。
- `change_log` 记录从 v1 到 v2 的变更、影响与回滚方式。

这个设计的意图是：**让维护者能读到"我们想表达什么"与"引擎实际能做什么"之间的差距**，而不是假装版本已经实现。

## 3. 证据 F15：一次 treaty 变更如何重述已报结果

将 `T-L1` 的 limit 由 50 改为 100：

| 层 | v1 区间 | v2 区间 |
| --- | --- | --- |
| L1 | [100, 150] | [100, 200] |
| L2 | [150, 250] | [150, 250] |

输入：`M1 = 120`、`M2 = 80`。

| 行 | 层 | v1 分摊 | v2 分摊 | 变化 |
| --- | --- | --- | --- | --- |
| M1 | L1 | 20 | 20 | 0 |
| M2 | L1 | 30 | **80** | **+50** |
| M2 | L2 | 50 | 50 | 0 |

`M2` 在 L1 上的分摊从 30 跳到 80，**增量 50 正好是 L1 拓宽的部分**。

### 关键观察：不只是"以后"变了

由于引擎整次运行只有一个数组，**v2 一启用，历史 movement（M1、M2）都会按 v2 重算**。

要验证这一点，可用登记的 `observed_reprice` 字段，或直接运行：

```bash
python3 tools/check_samples.py --list | grep F15
```

`F15` 的期望输出即为 v2 全量重算后的结果，与 `registers/treaty_change_register.json` 的 `observed_reprice` 一致。

## 4. 一个更锐利的反例：变更会追溯改写过去的 movement（F20）

样例 `F20-retroactive-restatement-narrowing` 把层 `L1` 由 `100 xs 100`（v1，区间 [100,200]）收窄为 `100 xs 20`（v2，区间 [100,120]），输入只有一条 `M1 = 150`：

| 版本 | M1 内 L1 分摊 |
| --- | --- |
| v1（`100 xs 100`） | 50（覆盖 [100,150]） |
| v2（`100 xs 20`） | **20**（只能覆盖 [100,120]） |

同一条**早已报出**的 M1，在 v2 下从 50 变成 20。这证明：**如果没有版本标签，历史结果会被追溯改写，且新旧结果无法区分。**

复现：

```bash
python3 tools/trace_allocation.py samples/F20-retroactive-restatement-narrowing.json
```

## 5. 生效日操作化的三条路径（均需权衡）

| 路径 | 做法 | 引擎能否支持 | 代价 |
| --- | --- | --- | --- |
| A. 全历史 + 单版本 | 每次变更后全量重算全部历史 | ✅ 能跑 | 历史结果被重述；需重新出账；无审计标记 |
| B. 拆分运行 | 按生效日把 movement 拆成两批，分别用 v1/v2 运行 | ✅ 能跑（外部拆分） | 引擎不读日期，拆分必须在 loader 完成；累计被切断（F11b 风险） |
| C. 期间化累计 + 多版本 | 引擎内部按时段选版本 | ❌ 不支持 | 需改代码（新增 effective 字段与期间累计） |

> **A 和 B 是变通，C 才是正解。** 交接文档必须允许维护者明确说"我们在用 A 或 B，并且知道其局限"。

## 6. 回滚

### 6.1 能回滚的部分

- **输入可回滚**：把 `treaties` 数组换回 v1，重跑即可得到 v1 结果。
- **回滚基线已固化为样例**：`F21-rollback-baseline-v1` 与 `F01` 输入相同，输出即为 v1 口径（L1: 20+30，L2: 50）。回滚后 F21 必须复现该值。
- **登记册可回滚**：`change_log` 中每条记录带 `rollback` 字段，写明恢复哪个版本。

### 6.2 不能回滚的部分

- **输出不带版本**：一次 v2 运行与一次 v1 运行的输出**结构完全相同**（同键、同形状）。事后仅凭输出无法判断该结果是哪个版本产生的。
- **下游已入账**：若 v2 结果已进入财务，回滚需要下游配合做冲正，引擎不提供"冲正凭据"。

因此回滚的**唯一可靠依据**是：

```
输入文件 + rule_version(R-809d7c1.1) + samples/manifest.json 的源文件哈希
```

这三者必须与每次运行结果一同留存。缺一不可。

### 6.3 回滚检查

```bash
# 1. 恢复到 v1 的 treaties 数组
# 2. 重跑受影响样例，应回到 v1 期望值
python3 tools/check_samples.py
# 3. F21 应回到 v1 口径（L1: 20+30, L2: 50）
python3 tools/trace_allocation.py samples/F21-rollback-baseline-v1.json
# 4. 若回滚后 F15 仍为 v2 结果，说明 treaties 未真正恢复
```

## 7. 未实现清单

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| `version` 字段 | NOT IMPLEMENTED | 引擎不读 |
| `effective_from/to` | NOT IMPLEMENTED | 登记册写为 `not-representable` |
| 按 movement 选版本 | NOT IMPLEMENTED | 一次运行一个数组 |
| 输出带版本标签 | NOT IMPLEMENTED | allocation 行只有 treaty name |
| 重叠/缺口校验 | NOT IMPLEMENTED | 见 F04、F05 |
| 年度累计限额 | NOT IMPLEMENTED | 只有本次运行的层宽 |
| share / 分出比例 | NOT IMPLEMENTED | 层隐含 100% |
| 恢复款/恢复次数 | NOT IMPLEMENTED | 见 F12 |

## 8. 交接结论

1. **不要把 `registers/treaty_change_register.json` 当作"版本已实现"的证据。** 它是意图登记册，`engine_representable: false` 才是事实。
2. 任何 treaty 变更都应走 `change_log`，并同时记录 `observed_reprice`，让下次维护者能看到"这条变更重述了哪条 movement 多少钱"。
3. 回滚预案必须包含"下游冲正"这一步，因为引擎不能自动撤销已发出的结果。
