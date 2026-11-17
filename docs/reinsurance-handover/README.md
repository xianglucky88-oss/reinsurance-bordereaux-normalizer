# 再保险数据交接资产包

本目录把经纪人字段映射、赔案 movement、treaty 版本、attachment/limit、恢复款、汇率与更正规则，从个人经验转成可复核、可回归的资产。

## 这个包解决什么问题

项目当前只有 ~126 行代码，实现了一条**累计损失跨层分摊**的最小链路。它没有 treaty 版本、没有币种、没有日期、没有文件身份、没有恢复款，也没有更正链接。交接的风险不是"代码写错了"，而是**维护者把引擎的输出当成业务结论，却不知道它在这些维度上是空白的**。

因此本包的每条结论都只允许落在两类之一：

- **已证明（PROVEN）**：由当前实现可执行地复现，附带样例 ID 与源码行号。
- **未实现（NOT IMPLEMENTED）**：业务需要、但代码中不存在；附证据（零命中检索）与影响样例。

不存在第三类"应该差不多是这样"的表述。

## 资产清单

| 文件 | 用途 | 是否机器可校验 |
| --- | --- | --- |
| `docs/reinsurance-handover/01-rule-baseline.md` | 引擎规则基线、逐行公式、规则版本号 | 是（样例） |
| `docs/reinsurance-handover/02-field-lineage.md` | 列级血缘、读取行号、边界 | 是（检索+样例） |
| `docs/reinsurance-handover/03-movement-and-claims.md` | 一赔案多 movement、跨层分摊、顺序敏感 | 是（样例） |
| `docs/reinsurance-handover/04-precision-and-fx.md` | 跨币种、跨精度、舍入漂移 | 是（样例） |
| `docs/reinsurance-handover/05-duplicates-and-corrections.md` | 重复文件、月度更正 | 是（样例） |
| `docs/reinsurance-handover/06-treaty-versioning.md` | treaty 有效期、重述、回滚 | 是（样例） |
| `docs/reinsurance-handover/07-compatibility-matrix.md` | 模板升级/treaty 生效/汇率修订/回滚 | 是（样例） |
| `docs/reinsurance-handover/08-review-checklist.md` | 上线前审查清单 | 人工 + 脚本 |
| `registers/bordereaux_field_lineage.json` | 字段血缘登记册（机器可读） | 是 |
| `registers/treaty_change_register.json` | treaty 变更登记册（机器可读） | 是 |
| `samples/*.json` | 28 个可复核输入样例 | 是 |
| `samples/manifest.json` | 样例的期望输出（角色快照） | 是 |
| `tools/check_samples.py` | 回归校验器 | — |
| `tools/trace_allocation.py` | 把一笔分摊追到源列与规则行 | — |

## 如何复现本包的全部数字

```bash
python3 tools/check_samples.py            # 28 个样例是否仍复现记录值
python3 tools/trace_allocation.py samples/F01-cross-layer-trace.json
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`check_samples.py` 退出码为 0 表示文档中的数字仍然成立。任何 `DRIFT` 行都意味着**某条已证明的边界已经过期**，必须同步修订本目录中引用该样例的段落。

## 规则版本

- `rule_version = R-809d7c1.1`，对应 commit `809d7c19bcdcac08332a970f2bc809707e532441`。
- 各源文件的 sha256 记录在 `samples/manifest.json` 的 `engine.files_sha256`。
- 引擎行为一旦改变，`check_samples.py` 会失败；此时应发布新 rule_version，而不是修改旧样例去迁就新行为。

## 阅读顺序建议

新任维护者按此顺序读，可在约 40 分钟内建立完整心智模型：

1. `01-rule-baseline.md` — 引擎到底做了什么（只有一个公式）。
2. `03-movement-and-claims.md` — 为什么顺序和赔案分组会改变结果。
3. `02-field-lineage.md` — 哪些列真的被读了（很少）。
4. `04` / `05` / `06` — 精度、重复与更正、版本这三个最容易踩坑的维度。
5. `07-compatibility-matrix.md` — 变更时逐项对照。
6. `08-review-checklist.md` — 上线前逐条签核。
