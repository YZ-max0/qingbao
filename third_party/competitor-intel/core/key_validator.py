"""
key_validator.py — API Key 实时预检验证（#33）

在用户填入 API Key 后，发送最小代价请求验证有效性：
  - OpenRouter：GET /api/v1/models（不消耗 Token，仅验证认证）
  - Tavily    ：POST /search，1 条结果（消耗 1 次配额，但最低限度）

全部使用标准库 urllib，无额外依赖。
返回 (ok: bool, message: str)：
  ok=True  → Key 有效，message 含附加信息（如可用模型数）
  ok=False → Key 无效或网络错误，message 含具体原因
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error

import config


def validate_openrouter(api_key: str, timeout: int = 20) -> tuple[bool, str]:
    """
    校验 LLM 密钥是否真正可用。

    本地改造：上游用 GET /api/v1/models 校验，但该端点**公开**，
    不携带密钥也返回 200 与全部模型列表 —— 实测 DeepSeek 真实 key、
    伪造 key、空 key 三者结果完全相同（HTTP 200, 462 models）。
    因此上游的"Key 预检"会把无效密钥报成有效，直到真正分析才失败。

    现改为发起一次最小化的 /chat/completions 请求（max_tokens=1）：
        401/403 -> 密钥无效或无权限
        402     -> 余额不足
        404     -> 模型名或 base_url 不正确
        429     -> 限流，密钥本身有效
        200     -> 真正可用
    """
    if not api_key or not api_key.strip():
        return False, "Key 为空"

    base = (config.OPENROUTER_BASE_URL or "").rstrip("/")
    if not base:
        return False, "未配置 LLM_BASE_URL"
    model = config.DEFAULT_MODEL

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return True, f"Key \u6709\u6548\uff08{model} \u53ef\u7528\uff09"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
            err = json.loads(body).get("error", {})
            detail = err.get("message", "") if isinstance(err, dict) else str(err)
        except Exception:
            pass
        hint = {
            401: "Key \u65e0\u6548\uff08401\uff09",
            402: "\u4f59\u989d\u4e0d\u8db3\uff08402\uff09\uff0c\u8bf7\u5145\u503c",
            403: "Key \u65e0\u6743\u9650\uff08403\uff09",
            404: f"\u6a21\u578b\u6216\u7aef\u70b9\u4e0d\u5b58\u5728\uff08404\uff09\uff1a{model}",
            429: "\u89e6\u53d1\u9650\u6d41\uff08429\uff09\uff0cKey \u672c\u8eab\u6709\u6548",
        }.get(e.code, f"HTTP \u9519\u8bef {e.code}")
        return (e.code == 429), hint + (f" \u2014 {detail[:80]}" if detail else "")
    except urllib.error.URLError as e:
        return False, f"\u7f51\u7edc\u9519\u8bef\uff1a{e.reason}"
    except Exception as e:
        return False, f"\u9a8c\u8bc1\u5931\u8d25\uff1a{e}"


def validate_tavily(api_key: str, timeout: int = 10) -> tuple[bool, str]:
    """
    调用 Tavily /search 接口验证 Key 有效性（最小查询，1 条结果）。
    成功时返回确认信息；失败时返回具体原因。
    """
    if not api_key or not api_key.strip():
        return False, "Key 为空"

    payload = json.dumps({
        "api_key": api_key.strip(),
        "query": "test",
        "max_results": 1,
        "search_depth": "basic",
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            result_count = len(data.get("results", []))
            return True, f"Key 有效（返回 {result_count} 条结果）"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:120]
        except Exception:
            pass
        if e.code == 401:
            return False, "Key 无效（401 Unauthorized）"
        if e.code == 400:
            # 400 无法确定 Key 本身是否有效，保守处理：提示用户检查格式
            if "invalid api" in body.lower() or "unauthorized" in body.lower():
                return False, f"Key 无效：{body}"
            return False, f"Key 格式可能有误（HTTP 400），请确认已完整复制"
        return False, f"HTTP 错误 {e.code}：{body}"
    except urllib.error.URLError as e:
        return False, f"网络错误：{e.reason}"
    except Exception as e:
        return False, f"验证失败：{e}"
