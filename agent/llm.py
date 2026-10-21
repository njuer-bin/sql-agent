"""LLM 客户端（OpenAI 兼容 chat completions）+ JSON 容错解析。

对应 PLAN.md 风险表「LLM API 不稳定 → agent 侧做 fallback」：
  - 环境变量 SQLAGENT_LLM_BASE_URL / SQLAGENT_LLM_API_KEY / SQLAGENT_LLM_MODEL任一未配置
    即视为 **LLM 未启用**，agent 自动走确定性 fallback（零 API 依赖）
  - chat_json 单次调用、内置 JSON 容错（代码围栏/前后噪声），
    解析失败返回 None 而不是抛异常
  - 全函数禁止 eval/exec/compile（代码审查红线）
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

FORBIDDEN_CALLABLES = ("eval", "exec", "compile", "__import__")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


class LLMConfig:
    """从环境变量读取的 LLM 配置；missing → enabled=False。"""

    def __init__(self) -> None:
        self.base_url: str = _env("SQLAGENT_LLM_BASE_URL")
        self.api_key: str = _env("SQLAGENT_LLM_API_KEY")
        model = _env("SQLAGENT_LLM_MODEL")
        self.model: str = model
        self.timeout_sec: float = float(_env("SQLAGENT_LLM_TIMEOUT_SEC", "60"))

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


def chat(messages: list[dict], cfg: LLMConfig | None = None, temperature: float = 0.0) -> str:
    """调用 OpenAI 兼容接口，返回 assistant 文本。网络/HTTP 错误抛 RuntimeError。"""
    cfg = cfg or LLMConfig()
    if not cfg.enabled:
        raise RuntimeError("LLM 未配置：请设置 SQLAGENT_LLM_BASE_URL/API_KEY/MODEL")

    url = cfg.base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"

    payload = json.dumps(
        {
            "model": cfg.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM 网络错误: {e.reason}") from e
    except TimeoutError as e:
        raise RuntimeError(f"LLM 请求超时（{cfg.timeout_sec}s）") from e

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"LLM 返回格式异常: {e}") from e


def extract_json(text: str) -> dict[str, Any] | None:
    """从容错地取出第一个 JSON 对象：
    1) 去掉 markdown 代码围栏  2) 用括号配对找 {...}  3) 解析失败返回 None
    """
    if text is None:
        return None

    s = text.strip()
    # 制造代码围栏
    if s.startswith("`"):
        lines = [line for line in s.splitlines() if not line.strip().startswith("```")]
        s = "\n".join(lines).strip()

    s = s.replace("```json", "").replace("```", "").strip()

    # 已是严格 JSON
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    # 括号配对兜底
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                cand = s[start : i + 1]
                try:
                    obj = json.loads(cand)
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None
