"""W5 MCP server：把工具层封装成 MCP（JSON-RPC 2.0 over stdio，最小实现）。

对应 PLAN.md W5「工具层封装成 MCP server」：
  - initialize / tools/list / tools/call 三个方法
  - sql_execute：只读 + 护栏 + 超时（复用 ToolBox）
  - chart_render：把 (label, value) 数据转成 SVG 柱状图（无第三方依赖）
  - metric_lookup：语义记忆里的口径条目
  - 写操作（INSERT/UPDATE/DELETE/...）无论 approved 与否都会被护栏拦截；
    增加了 sql_write 工具：approved=False 一律拒（Human-in-the-loop 阈值）

传输协议：每行一个 JSON-RPC 2.0 请求，返回一行 JSON-RPC 响应。
"""

from __future__ import annotations

import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.errors import GuardrailError
from agent.guardrails import (_DANGEROUS_PATTERNS, _MULTI_LINE_COMMENT,
                              _SENSITIVE_FIELD_RE, _SINGLE_LINE_COMMENT,
                              _strip_for_check)
from agent.memory import DEFAULT_MEMORY_PATH, MemoryStore
from agent.tools import ToolBox

_SERVER_INFO = {"name": "sql-agent-tools", "version": "0.5.0"}


def _strip_comments(sql: str) -> str:
    """写工具专用：只剥注释、保留字符串（敏感数据藏在 VALUES 字符串里）。"""
    s = _SINGLE_LINE_COMMENT.sub(" ", sql)
    return _MULTI_LINE_COMMENT.sub(" ", s)


_WRITE_ALLOWED = re.compile(r"\bINSERT\b", re.I)

_WRITE_REDLINES = [p for p in _DANGEROUS_PATTERNS
                   if not re.fullmatch(r"\\bINSERT\\b", p)]


def check_write_redlines(sql: str) -> str | None:
    """写工具专用：危险模式/权限/结构变更在注释剥壳骨架上查（防藏在注释里）。"""
    skeleton = _strip_for_check(sql)
    for pat in _WRITE_REDLINES:
        if re.search(pat, skeleton, re.I):
            return "命中护栏红线（DELETE/DROP/结构变更/权限等），审批也不可越过"
    if not _WRITE_ALLOWED.search(skeleton):
        return "只能提交普通写操作（INSERT）"
    return None


def _tool_write(sql: str, approved: bool = False,
                toolbox: ToolBox | None = None) -> dict:
    """写操作工具：Human-in-the-loop —— 红线 → 审批 → 只读环境，层层拦截。"""
    if not sql or not sql.strip():
        return {"ok": False, "blocked": True, "error": "empty_sql",
                "detail": "SQL 为空"}
    red = check_write_redlines(sql)
    if red:
        return {"ok": False, "blocked": True, "error": "guardrail_rejected",
                "detail": red}
    if _SENSITIVE_FIELD_RE.search(_strip_comments(sql)):
        return {"ok": False, "blocked": True, "error": "sensitive_field",
                "detail": "涉及敏感字段（个人信息），禁止写入本工具"}
    if re.search(r"\b1[3-9]\d{9}\b", _strip_comments(sql)):
        return {"ok": False, "blocked": True, "error": "sensitive_field",
                "detail": "疑似手机号等个人数据（11 位手机号模式），禁止写入本工具"}
    if not approved:
        return {"ok": False, "blocked": True, "error": "approval_required",
                "detail": "写操作需要人工审批（approved=true），已拦截。"}
    return {"ok": False, "blocked": True, "error": "readonly_environment",
            "detail": "本环境数据库为只读连接（mode=ro），审批通过与红线检查"
                      "都通过也不会执行写操作；如需写入请走专门 ETL 流程并留"
                      "人工审批记录。"}


def chart_render(labels: list[str], values: list[float],
                 title: str = "", kind: str = "bar") -> dict:
    """极简 SVG 柱状图（无第三方依赖，跨进程可交付）。"""
    if not labels or len(labels) != len(values):
        return {"ok": False, "error": "labels/values 长度不一致"}
    w, h, pad = 60 * len(labels) + 40, 220, 30
    vmax = max(abs(float(v)) for v in values) or 1.0
    bars, texts = [], []
    for i, (lab, v) in enumerate(zip(labels, values)):
        bh = max(1.0, 160.0 * abs(float(v)) / vmax)
        x = pad + i * 60
        bars.append(f'<rect x="{x}" y="{h - pad - int(bh)}" width="40" '
                    f'height="{int(bh)}" fill="#4a6" />')
        texts.append(f'<text x="{x + 2}" y="{h - pad - int(bh) - 4}" font-size="11">'
                     f'{float(v):.4g}</text><text x="{x}" y="{h - 10}" '
                     f'font-size="11">{lab[:8]}</text>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
           f'<text x="{pad}" y="16" font-size="13">{title}</text>'
           f'<line x1="{pad - 10}" y1="{h - pad}" x2="{w - 10}" y2="{h - pad}" '
           f'stroke="#888" />' + "".join(bars) + "".join(texts) + "</svg>")
    return {"ok": True, "svg": svg, "kind": kind}


_TOOLS = [
    {"name": "sql_execute",
     "description": "执行只读 SQL（护栏+超时）",
     "inputSchema": {"type": "object",
                     "properties": {"sql": {"type": "string"}},
                     "required": ["sql"]}},
    {"name": "chart_render",
     "description": "把 (label,value) 数据渲染为 SVG 柱状图",
     "inputSchema": {"type": "object",
                     "properties": {"labels": {"type": "array"},
                                    "values": {"type": "array"},
                                    "title": {"type": "string"}}}},
    {"name": "metric_lookup",
     "description": "查指标口径（语义记忆条目）",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"}}}},
    {"name": "sql_write",
     "description": "写操作（无审批一律拒）",
     "inputSchema": {"type": "object",
                     "properties": {"sql": {"type": "string"},
                                    "approved": {"type": "boolean"}}}},
]


def _dispatch(method: str, params: dict, toolbox: ToolBox,
              store: MemoryStore) -> tuple[dict | list | str, bool]:
    """返回 (result, is_error)。"""
    if method == "initialize":
        return {"serverInfo": _SERVER_INFO,
                "protocolVersion": "2024-11-05"}, False
    if method == "tools/list":
        return {"tools": _TOOLS}, False
    if method == "tools/call":
        name = str(params.get("name", ""))
        args = params.get("arguments") or {}
        if name == "sql_execute":
            try:
                res = toolbox.sql_execute(str(args.get("sql", "")))
                return {"content": [{"type": "json", "json": res}]}, False
            except Exception as e:  # noqa: BLE001  错误也按结构返回
                return {"content": [{"type": "text", "text": str(e)}],
                        "isError": True}, True
        if name == "chart_render":
            try:
                return {"content": [{"type": "json", "json": chart_render(
                    list(args.get("labels") or []),
                    list(args.get("values") or []),
                    str(args.get("title", "")) or "")}]}, False
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": str(e)}],
                        "isError": True}, True
        if name == "metric_lookup":
            hits = store.retrieve(str(args.get("query", "")), top_k=3)
            return {"content": [{"type": "json",
                                 "json": store.dump([e for e in hits
                                                     if e.get("memory_type")
                                                     == "semantic"])}]}, False
        if name == "sql_write":
            try:
                res = _tool_write(str(args.get("sql", "")),
                                  bool(args.get("approved", False)), toolbox)
                return {"content": [{"type": "json", "json": res}]}, False
            except Exception as e:  # noqa: BLE001
                return ({"content": [{"type": "text", "text": str(e)}],
                         "isError": True}, True)
        return ({"content": [{"type": "text", "text": f"未知工具: {name}"}],
                 "isError": True}, True)
    return ({"content": [{"type": "text", "text": f"未知方法: {method}"}],
             "isError": True}, True)


def serve(stdin=None, stdout=None) -> None:  # pragma: no cover - 供 client 集成测
    """主循环：每行一个请求（生产由 MCP 框架接入，这里是最小可测实现）。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    toolbox = ToolBox()
    store = MemoryStore(DEFAULT_MEMORY_PATH)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            method = str(req.get("method", ""))
            params = req.get("params") or {}
            result, is_error = _dispatch(method, params, toolbox, store)
            resp = {"jsonrpc": "2.0", "id": rid, "result": result}
            if is_error:
                resp["result"]["isError"] = True
        except Exception as e:  # noqa: BLE001
            resp = {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32603, "message": str(e)}}
        stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
        stdout.flush()


if __name__ == "__main__":
    serve()
