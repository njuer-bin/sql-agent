# 评测框架（eval/）

给「多智能体经营分析助手」打分的基准测试框架。**46 道题、8 个类别、可复现、零 API 依赖**。

---

## 1. 为什么需要它

面试官一天看十个"我做了个 RAG 知识库"，绝大多数只能说"效果不错"。
这个框架让你能说出可验证的话：

> "口径类问题准确率从 0% 提到 100%（加记忆模块前后）；幻觉率 0%；
> SQL 可执行率从 68% 提到 100%（加纠错循环前后）。"

**关键设计**：所有 gold 答案都由 `warehouse.db` 真实数据算出，不是人编的。
所以换业务、换数据库，只要重跑一次生成器，题目和答案自动跟着变。

---

## 2. 文件地图

| 文件 | 作用 |
|---|---|
| `build_questions.py` | **题目生成器**：从数据库算 gold 答案，输出 8 个 jsonl |
| `questions/*.jsonl` | 46 道题（生成产物，不要手改） |
| `metrics.yaml` | 指标字典：12 个指标的口径、别名、歧义、版本链 |
| `memory_fixtures.jsonl` | 19 条记忆种子（语义 9 / 情节 6 / 程序 4） |
| `verifier.py` | **评分器**：四层分项给分 + 幻觉检测 + 汇总 |
| `adapters.py` | **适配器层**：被测 agent 与框架之间的唯一边界 |
| `runner.py` | **执行器**：跑题、打分、落盘 |
| `report.py` | **报告器**：出消融对比表 |
| `results/*.json` | 跑分产物 |

---

## 3. 快速开始

```bash
# 0) 准备数据（首次）
python data/warehouse/seed.py
python data/warehouse/verify_effects.py     # 6 个效应必须全 PASS

# 1) 生成题目 + 自校验
python eval/build_questions.py --check

# 2) 跑分（三组对照）
python eval/runner.py --compare oracle rule-baseline null

# 3) 出报告
python eval/report.py oracle rule-baseline null
```

---

## 4. 题目分类（8 类 46 题）

| 类别 | 题数 | 题型 | 考察点 |
|---|---:|---|---|
| `a_basic` | 8 | data | 单表取数（埋了 cancelled 陷阱） |
| `b_join` | 8 | data | 多表关联（地区/渠道/类目/新老客） |
| `c_metric` | 6 | memory | **口径题** —— 语义记忆 |
| `d_episode` | 6 | memory | **复用题** —— 情节记忆 |
| `e_procedure` | 4 | attribution | **套路题** —— 程序记忆 |
| `f_conflict` | 4 | memory | **冲突题** —— 版本链消解 |
| `g_attribution` | 6 | attribution | **归因推理** —— 必须查数据 |
| `h_long_horizon` | 4 | memory | **长程记忆** —— 50 轮之后还记得吗 |

每道题的 `expected` 里可能带：

- `columns` / `result` / `ordered` / `tolerance` —— 数值 gold
- `reference_sql` —— gold SQL（可执行、可复现）
- `answer_contains` / `must_mention` / `answer_forbidden` —— 结论文本 gold
- `evidence_required` —— 归因题的证据链要求
- `memory_ids` —— **期望命中的记忆条目**（检索命中率的分子）

---

## 5. 评分口径

四层独立给分，避免"全对或全错"：

| 分项 | 含义 | 适用题型 | 权重 |
|---|---|---|---:|
| `execution` | SQL 能不能跑 | data / attribution | 0.15 / 0.10 |
| `schema` | 列名对不对 | data | 0.15 |
| `result` | 数值对不对（含部分分） | data / attribution | 0.35 / 0.30 |
| `answer` | 结论对不对 | 全部 | 0.35 / 0.45 |
| `evidence` | 证据链全不全 | attribution | 0.15 |
| `retrieval` | 记忆召回率 | memory | 0.30 |

**幻觉检测**：答案里的每个数字都必须能在工具输出里找到出处，否则判定幻觉并**硬惩罚**（`final × 0.5`）。

**汇总口径**（重要）：不同分项只在有意义的子集上求均值 ——
`result_acc` 只在 data/attribution 上算，`retrieval_acc` 只在 memory 上算。
否则会被没有该分项的题型稀释成假数字。

---

## 6. 三个内置 adapter

| adapter | 是什么 | 期望分数 | 用途 |
|---|---|---:|---|
| `oracle` | 直接返回 gold 答案 | **1.000** | **验证框架本身没写错** |
| `null` | 什么都不答 | ≈ 0 | 验证分数下限，不白送分 |
| `rule-baseline` | 固定模板生成 SQL，不查记忆 | ≈ 0.2 | 消融实验的**对照组** |

`oracle` 是天花板也是自检工具：**它拿不到 1.000 就说明 verifier 或题目有 bug**。

---

## 7. 接入你自己的 agent

实现一个类，放进 `adapters.py` 的 `ADAPTERS` 注册表即可：

```python
class MyAgent:
    name = "my-agent"

    def answer(self, question: dict, ctx: RunContext) -> AgentOutput:
        # ctx.sql()            只读 SQL 执行
        # ctx.search_memory()  记忆检索
        # ctx.schema()         建表语句
        ...
        return AgentOutput(answer=..., sql=..., result=..., ...)
```

然后：

```bash
python eval/runner.py --compare rule-baseline my-agent
```

**不要**把 agent 实现写进 `runner.py` 或 `verifier.py` —— 那样换 agent 就得改评测逻辑。

---

## 8. 已知设计约束（踩过的坑）

1. **禁止项不能是必需项的肯定形式子串**。真实踩过：`must_mention: 不是老客流失`
   包含 `answer_forbidden: 是老客流失`，导致正确答案被判违规。
   现在 `build_questions.py --check` 会自动拦截，`verifier` 也会做否定语境判断。
2. **空答案不能送分**。题目没设 `answer_contains` 时不代表可以得满分。
3. **汇总要分子集**，否则 baseline 分数会被"没定义该分项的题"拉低/拉高。
4. **qid 按类别编号**（`C-001` 起），不要改成全局编号 —— `memory_ids` 映射依赖它。
