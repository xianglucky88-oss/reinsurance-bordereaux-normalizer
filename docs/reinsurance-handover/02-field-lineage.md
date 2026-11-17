# 02 · 字段血缘

> rule_version `R-809d7c1.1` · 机器可读版：`registers/bordereaux_field_lineage.json`

## 1. 先说结论：引擎实际只读取 6 个字段

`losses` 容器告诉你"有几行"；真正被读取的字段值是 6 个：`claim`、`movement`、`amount`、`treaties[].name`、`treaties[].attachment`、`treaties[].limit`。第 7 个列 `currency` 被接受但**从不读取**。对一个声称处理 bordereaux 的系统，这个数字是理解全部风险的起点。

| 归一化字段 | 来源 | 读取位置 | 状态 | 是否影响计算 |
| --- | --- | --- | --- | --- |
| `losses`（容器） | `payload.losses` | parser.py:2 | IMPLEMENTED | 决定行数 |
| `claim` | `losses[].claim` | engine.py:9 | IMPLEMENTED | **否**，仅回写标签 |
| `movement` | `losses[].movement` | engine.py:9 | IMPLEMENTED | **否**，仅回写标签 |
| `amount` | `losses[].amount` | engine.py:4 | IMPLEMENTED | **是**，唯一的数值输入 |
| `currency` | `losses[].currency` | — | **READ_UNUSED** | 否 |
| `treaties[].name` | `treaties[].name` | engine.py:9 | IMPLEMENTED | 否，仅回写标签 |
| `treaties[].attachment` | `treaties[].attachment` | engine.py:2,6,7 | IMPLEMENTED | **是**（排序 + 下界） |
| `treaties[].limit` | `treaties[].limit` | engine.py:6,7 | IMPLEMENTED | **是**（层宽） |

> 注意：`currency` 虽在样例中出现，但代码中**零引用**。它既不影响计算，也不出现在输出里。删除或重命名它不改变任何结果（F07、F13 已验证）。

## 2. 真正的计算输入只有两个

把上表按"是否改变数字"重新投影：

- `amount`：唯一的损失数值来源，经 `float()` 强转后可正可负。
- `attachment` + `limit`：唯一的层定义来源，两者相加得到层顶。

`claim`、`movement`、`name` 三者只影响**输出行上打印什么标签**，不影响任何金额。这解释了一个反直觉现象：把 movement 改名（M1→TXN1）不会改变分摊，但**把同样的行换个顺序**会（见 `03`）。

## 3. 逐列边界

### 3.1 `amount`

- 缺失 → `KeyError: 'amount'`（engine.py:4）。
- 数值字符串 `"120"` → 静默变 `120.0`（F13）。
- 非数值文本 → `ValueError: could not convert string to float`（E04）。
- JSON `null` → `TypeError`。
- 符号不校验：负数一律视为摊回（F12）。

**未实现**：分币种、分 sign 语义（paid vs recovery 类型）、分精度标度。

### 3.2 `attachment` / `limit`

- `limit` 是**层宽**，层顶 = `attachment + limit`。这是最容易误配的一点。
- `attachment` 为字符串 → 排序时 `TypeError`，**整次运行失败**（E02）。
- `limit` 为字符串而 `attachment` 为数字 → 加法处 `TypeError`（engine.py:6）。
- `limit = 0` 或负数 → 空区间，静默无 allocation。
- 不校验重叠、不校验缺口、不校验负 attachment。
- 相等 attachment → 保留输入顺序（F14）。

**未实现**：`top` 作为独立列、order/priority 列、层的生效日。

### 3.3 `claim` / `movement`

- 二者均只回写。`claim` 不用于分组（F02），`movement` 不用于排序（F03a/F03b）。
- 缺失分别在 engine.py:9 抛 `KeyError`。
- 对 treaty 层：若该层不产生非零金额，其 `name` 缺失**不会**报错——这是一个漏检点。

### 3.4 `currency`

- 零引用。多币种直接相加：`120 USD + 80 EUR = 200`（F07）。
- 输出信封不含币种，下游无法还原每条 allocation 的计价币种。

**未实现**：FX 汇率、汇率日、结算币种、币种校验。

## 4. 未实现的业务列（NOT IMPLEMENTED）

以下均为业务必需、但代码中零命中。证据为对 `src/` 的 `grep -rniw`。

| ID | 概念 | 经纪人口径示例 | 检索证据 | 影响样例 |
| --- | --- | --- | --- | --- |
| NI-FX | 汇率 | `fx_rate`, `exchange_rate`, `rate_date` | `fx\|rate\|exchange` → 0 | F07 |
| NI-TREATY-VERSION | treaty 版本/批单 | `treaty_version`, `endorsement`, `effective_from` | `version\|effective` → 0 | F15 |
| NI-LOSS-DATE | 损失日/账期 | `loss_date`, `advice_date`, `period`, `month` | `date\|month\|period` → 0 | F11a/b |
| NI-FILE-IDENTITY | 文件身份 | `file_id`, `statement_no`, `batch_id` | `file_id\|hash\|batch\|dedup` → 0 | F10 |
| NI-CEDED-SHARE | 分出比例 | `ceded_pct`, `quota_share`, `participation` | `ceded\|share\|percent` → 0 | — |
| NI-REINSTATEMENT | 恢复款 | `reinstatement_premium`, `reinstatement_count` | `reinstat\|premium` → 0 | F12 |
| NI-CORRECTION-LINK | 更正链接 | `corrects_movement`, `supersedes`, `reversal_of` | `correction\|supersede\|reversal` → 0 | F03a/b |

这七项构成了"把个人经验变成资产"时**必须显式标注为缺口**的部分。它们不是配置项，引擎没有任何开关能启用。

## 5. 维护者如何追踪一笔分摊到原始列

用 `tools/trace_allocation.py`：

```bash
python3 tools/trace_allocation.py samples/F01-cross-layer-trace.json --claim C1 --treaty L2
```

它会打印：
1. 本文件用到的**源列及其来源**（含"currency 未读取"的显式提示）。
2. 排序后的层区间。
3. 每一行的 `start → cumulative` 变化，以及每个层的 `covered_before/after`。
4. 每条公式对应的**源码文件:行号**。
5. 独立重算结果与引擎输出的比对。

维护者由此可以从一条 `allocated` 数字，沿"源列 → 累加 → 层区间 → 舍入 → 输出字段"的链条反向定位，而不依赖个人记忆。

## 6. 升级经纪人模板时的最小检查

模板改动只可能落在三类：

| 改动类型 | 引擎反应 | 需要的动作 |
| --- | --- | --- |
| 增加列 | 忽略（F13 扩展列已验证） | 无；但新列不会进入计算 |
| 删除/重命名 `amount`/`claim`/`movement`/`name`/`attachment`/`limit` | `KeyError`，运行失败 | 在 loader 层做映射，引擎不能直接吃新模板 |
| 修改 `currency` | **无反应** | 不要误以为改币种列会触发换算 |

详见 `07-compatibility-matrix.md`。
