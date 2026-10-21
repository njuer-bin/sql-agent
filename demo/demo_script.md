# 5 分钟完整演示脚本（demo/run_demo.py 自动跑，本文件是讲稿）

前提：`warehouse.db` 已由 `eval/build_questions.py` 之外的建库脚本就绪
（本仓库自带 `data/warehouse/warehouse.db`）。

## Demo 流程（离线模式即可演示，全程不卡壳）

| 分钟 | 动作 | 预期 |
|---|---|---|
| 0:00–0:40 | `python demo/run_demo.py basic` | 简单取数：SQL + 结果 + 结论（a_basic） |
| 0:40–1:40 | `python demo/run_demo.py error` | 故意喂错 SQL（错误列名），看 W2 纠错循环逐次修复留痕 |
| 1:40–2:40 | `python demo/run_demo.py memory` | 口径问题："客单价分母是什么" → 检索引用原文作答 |
| 2:40–4:00 | `python demo/run_demo.py attribution` | 归因题：9 月华北退款率 → 定位到 2026-09-10~14 物流事故窗口 + 反向验证 |
| 4:00–5:00 | 打开 `eval/traces/<qid>.json` | 全链路 Tracing：每一步工具调用/检索/纠错可回放，`locate_failure` 可定位出错步骤 |

## 演示要点

- 每一步都有工具输出留痕——任何数字都能指出处（防幻觉的机制而非性格）。
- 对抗 Verifier 与 planner 不共享上下文：开发期真的拦下过自家「7 步」数字无出处的问题。
- 写操作无审批一律拦截（demo `mcp` 演示）。

## 可选（如果环境里有 streamlit）

    streamlit run app/streamlit_app.py
    → 提问 → SQL/结果表/结论/置信度 → 「一键复核」重跑独立验证
