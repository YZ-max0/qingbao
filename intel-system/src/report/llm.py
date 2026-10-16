"""LLM 提炼层 —— 把情报条目摊开，总结成几句话。

产出三样东西（对应看板右侧「提炼」区）：
    llm_summary : 2~4 句的整体介绍（来龙去脉、关键数据、结论）
    llm_digest  : 一句话（列表首行，替代规则层）
    llm_impact  : 一句「对我意味着什么」

设计要点：
  - 未配置 API key 时整体静默降级到规则层，不影响主流程
  - 优先使用抓取到的正文（body）；无正文时退回标题，并提示模型不要编造
  - 结果落库，重复运行不重复付费（按 llm_summary 是否为空判断）
  - 小批量并发受限，避免触发限流
"""
from __future__ import annotations

import json
import os

from ..common import db
from ..common.util import CONFIG_DIR, load_yaml, setup_logging

log = setup_logging("llm")

BATCH = 5
LOCAL_CONF = CONFIG_DIR / "llm.local.yaml"

# 支持任何兼容 OpenAI 协议的服务
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM = """你是制造业/仓储机器人行业的竞品情报分析师，为一家做仓储AMR（料箱机器人、
货到人、无人叉车、仓储集成）的公司服务。

对每一条情报，输出：
1. summary：把这条信息摊开讲清楚，2~4 句话。写清「谁、做了什么、关键数字/结论、
   背景原因」。让读者不用点开原文就了解全貌。
2. digest：一句话概括（不超过 50 字），用于列表首行。
3. impact：一句「对我意味着什么」，站在仓储AMR厂商立场，不超过 40 字。

硬性要求：
- 只依据给定内容，绝不编造数字、公司名或事实
- 若给定内容不足以支撑 summary，就基于标题写你确定的部分，宁短勿编
- 平实直述，禁用"据悉""值得关注""引发热议"这类空话
- 中文输出

返回严格 JSON：{"items":[{"i":序号,"summary":"...","digest":"...","impact":"..."}]}"""


def _conf() -> dict:
    """读取 LLM 配置。环境变量优先于本地配置文件。"""
    conf: dict = {}
    if LOCAL_CONF.exists():
        try:
            conf = load_yaml(LOCAL_CONF) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 %s 失败: %s", LOCAL_CONF.name, exc)
            conf = {}

    if conf.get("enabled") is False:
        conf["api_key"] = ""
        return conf

    # 环境变量覆盖文件（便于临时切换而不改文件）
    if os.environ.get("OPENAI_API_KEY"):
        conf["api_key"] = os.environ["OPENAI_API_KEY"]
    if os.environ.get("OPENAI_BASE_URL"):
        conf["base_url"] = os.environ["OPENAI_BASE_URL"]
    if os.environ.get("OPENAI_MODEL"):
        conf["model"] = os.environ["OPENAI_MODEL"]

    conf.setdefault("base_url", DEFAULT_BASE_URL)
    conf.setdefault("model", DEFAULT_MODEL)
    conf.setdefault("batch_size", BATCH)
    conf.setdefault("max_body_chars", 3000)
    conf.setdefault("timeout", 180)
    return conf


def enabled() -> bool:
    return bool((_conf().get("api_key") or "").strip())


def status() -> str:
    """人类可读的配置状态，便于启动时打印。"""
    c = _conf()
    if not (c.get("api_key") or "").strip():
        if LOCAL_CONF.exists() and c.get("enabled") is False:
            return f"已在 {LOCAL_CONF.name} 中禁用"
        return f"未配置密钥（环境变量 OPENAI_API_KEY 或 {LOCAL_CONF.name}）"
    src = "环境变量" if os.environ.get("OPENAI_API_KEY") else LOCAL_CONF.name
    return f"已启用（来源: {src}，模型: {c.get('model')}）"


def _call(payload: list[dict]) -> dict[int, dict]:
    import httpx

    c = _conf()
    key = c["api_key"]
    base = c["base_url"].rstrip("/")
    model = c["model"]
    max_body = int(c.get("max_body_chars", 3000))

    items = []
    for n, p in enumerate(payload):
        body = (p.get("body") or "").strip()
        items.append({
            "i": n,
            "标题": (p.get("title") or "")[:200],
            "频道": p.get("channel") or "",
            "时间": p.get("published_at") or p.get("captured_at") or "",
            "已有信息": (p.get("summary") or "")[:300],
            # 控制正文长度：取前 N 字足够概括，也省 token
            "正文": body[:max_body] if body else "（未抓取到正文）",
        })

    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        timeout=float(c.get("timeout", 180)),
    )
    resp.raise_for_status()
    data = json.loads(resp.json()["choices"][0]["message"]["content"])

    arr = data.get("items") if isinstance(data, dict) else data
    if arr is None and isinstance(data, dict):
        arr = next((v for v in data.values() if isinstance(v, list)), [])

    out: dict[int, dict] = {}
    for it in arr or []:
        try:
            out[int(it["i"])] = {
                "summary": str(it.get("summary", "")).strip()[:1200],
                "digest": str(it.get("digest", "")).strip()[:300],
                "impact": str(it.get("impact", "")).strip()[:200],
            }
        except (KeyError, ValueError, TypeError):
            continue
    return out


def run(limit: int = 100, channels: tuple[str, ...] = ("media",),
        redo: bool = False) -> dict[str, int]:
    """为指定频道生成 LLM 提炼。

    redo=True 时忽略已有结果重做（改提示词后用于刷新）。
    """
    if not enabled():
        log.info("未配置 LLM 密钥，跳过提炼（%s）", status())
        return {"done": 0, "failed": 0, "skipped": 1}

    conf = _conf()
    batch = int(conf.get("batch_size", BATCH))

    ph = ",".join("?" * len(channels))
    cond = "" if redo else "AND COALESCE(llm_summary,'')=''"
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT id,title,channel,summary,body,published_at,captured_at
                FROM events
                WHERE channel IN ({ph}) {cond}
                ORDER BY COALESCE(NULLIF(published_at,''),captured_at) DESC
                LIMIT ?""",
            list(channels) + [limit],
        ).fetchall()

    todo = [dict(r) for r in rows]
    if not todo:
        log.info("没有待提炼的条目")
        return {"done": 0, "failed": 0, "skipped": 0}

    withbody = sum(1 for r in todo if (r.get("body") or "").strip())
    log.info("待 LLM 提炼: %d 条（其中 %d 条有正文）", len(todo), withbody)

    stats = {"done": 0, "failed": 0, "retried": 0}
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        res = _call_with_retry(chunk, start // batch + 1, stats)
        if res is None:
            stats["failed"] += len(chunk)
            continue

        with db.connect() as conn:
            for n, item in enumerate(chunk):
                r = res.get(n)
                if not r or not (r.get("summary") or r.get("digest")):
                    stats["failed"] += 1
                    continue
                conn.execute(
                    "UPDATE events SET llm_summary=?, llm_digest=?, llm_impact=?,"
                    " updated_at=? WHERE id=?",
                    (r.get("summary", ""), r.get("digest", ""), r.get("impact", ""),
                     db.now_iso(), item["id"]),
                )
                stats["done"] += 1
        log.info("进度 %d/%d", min(start + batch, len(todo)), len(todo))

    log.info("LLM 提炼完成: %s", stats)
    return stats


# 可重试的服务端/网络错误特征。503/502/429 与超时都是暂时性的，
# 等一会儿通常能恢复；401/402/404 是配置问题，重试无意义。
_RETRYABLE_HINTS = ("503", "502", "504", "429", "Service Unavailable",
                    "Temporarily", "timeout", "timed out", "Connection")


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc)
    return any(h.lower() in msg.lower() for h in _RETRYABLE_HINTS)


def _call_with_retry(chunk: list[dict], batch_no: int,
                     stats: dict, max_retries: int = 2) -> dict | None:
    """带指数退避的批次调用。

    背景（实测）：2026-10-15 定时任务中 DeepSeek 返回 503，
    原有逻辑「整批放弃」导致那批新增稿件的提炼全部丢失（次日才会补上）。
    这里改为对临时性错误退避重试，避免整批白跑。

    不可重试的错误（密钥无效、余额不足、模型名错）直接放弃，
    避免无谓等待与配额浪费。
    """
    import time

    delay = 5.0
    for attempt in range(max_retries + 1):
        try:
            return _call(chunk)
        except Exception as exc:  # noqa: BLE001
            retryable = _is_retryable(exc)
            if not retryable or attempt >= max_retries:
                log.warning("第 %d 批失败（%s）: %s",
                            batch_no,
                            "已达最大重试" if retryable else "不可重试错误",
                            str(exc)[:140])
                return None
            stats["retried"] = stats.get("retried", 0) + 1
            log.warning("第 %d 批遇到临时错误（%s），%.0fs 后重试 %d/%d",
                        batch_no, str(exc)[:70], delay, attempt + 1, max_retries)
            time.sleep(delay)
            delay *= 2      # 5s -> 10s
    return None
