"""给深度分析报告生成要点提炼（3~5 句），供看板右侧栏展示。

背景：
    reports.strategy 字段是从报告中**截取的段落**（3000~4000 字），
    直接展示在列表右侧根本读不完，而且截断处语义不完整。
    competitor-intel 生成的报告本身有 4000~6000 字，需要真正压缩。

产出：
    reports.brief     —— 3~5 句要点（定位 / 威胁 / 行动）
    reports.brief_at  —— 生成时间

成本：每份约 ¥0.01，57 份约 ¥0.6。
幂等：已有 brief 的默认跳过，--redo 强制重做。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common import db  # noqa: E402
from src.report import llm as llm_mod  # noqa: E402
from src.common.util import setup_logging  # noqa: E402

log = setup_logging("report_brief")

BATCH = 5

SYSTEM = """你是制造业/仓储机器人行业的竞品情报分析师，为一家做仓储AMR
（料箱机器人、货到人、无人叉车、仓储集成）的公司服务。

给你一份完整的竞品分析报告，请压缩成**3~5 句要点**，供决策者快速浏览。

输出格式（严格按此结构，每部分 1~2 句，总长不超过 220 字）：
定位：<这家竞品是谁、做什么、核心优势>
威胁：<它对我方构成什么具体威胁>
行动：<我方本季度该做什么>

硬性要求：
- 只依据报告内容，不得编造数字、公司名或结论
- 报告未提及的信息就略过，宁短勿编
- 用陈述句，禁用"据悉""值得关注""引发热议"这类空话
- 保留报告中的关键数字（市占率、融资金额、客户数等）
- 中文输出

返回严格 JSON：{"brief":"定位：…\\n威胁：…\\n行动：…"}"""


def _call(report: str, competitor: str) -> str:
    import httpx

    conf = llm_mod._conf()
    key = conf["api_key"]
    base = conf["base_url"].rstrip("/")
    model = conf["model"]

    # 报告较长，取全文但设上限（6000 字足够覆盖核心结论）
    body = report[:6000]

    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content":
                    f"竞品：{competitor}\n\n以下是完整报告：\n\n{body}"},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "max_tokens": 1200,
        },
        timeout=180,
    )
    resp.raise_for_status()
    data = json.loads(resp.json()["choices"][0]["message"]["content"])
    return (data.get("brief") or "").strip()


def _call_with_retry(report: str, competitor: str, stats: dict,
                     max_retries: int = 2) -> str:
    """带指数退避的单份报告提炼。

    背景（实测）：2026-10-15 定时任务中 DeepSeek 返回 503，
    原逻辑直接放弃该份报告，导致其 brief 缺失（次日才会补上）。
    这里对临时性错误退避重试；配置类错误（401/402/404）直接放弃。

    复用 llm 模块的可重试判定，避免两处规则不一致。
    """
    import time

    from .llm import _is_retryable

    delay = 5.0
    for attempt in range(max_retries + 1):
        try:
            return _call(report, competitor)
        except Exception as exc:  # noqa: BLE001
            retryable = _is_retryable(exc)
            if not retryable or attempt >= max_retries:
                log.warning("%s 提炼失败（%s）: %s", competitor,
                            "已达最大重试" if retryable else "不可重试错误",
                            str(exc)[:140])
                return ""
            stats["retried"] = stats.get("retried", 0) + 1
            log.warning("%s 遇到临时错误（%s），%.0fs 后重试 %d/%d",
                        competitor, str(exc)[:60], delay, attempt + 1, max_retries)
            time.sleep(delay)
            delay *= 2
    return ""


def run(limit: int = 100, redo: bool = False) -> dict[str, int]:
    if not llm_mod.enabled():
        log.warning("未配置 LLM 密钥，跳过（%s）", llm_mod.status())
        return {"done": 0, "failed": 0, "skipped": 1}

    db.init_db()

    cond = "" if redo else "WHERE COALESCE(brief,'')=''"
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT id, competitor, report FROM reports {cond} "
            f"ORDER BY id LIMIT ?", (limit,)
        )]

    if not rows:
        log.info("没有待提炼的报告")
        return {"done": 0, "failed": 0, "skipped": 0}

    log.info("待提炼报告: %d 份", len(rows))
    stats = {"done": 0, "failed": 0, "retried": 0}
    for r in rows:
        brief = _call_with_retry(r["report"], r["competitor"], stats)
        if not brief:
            stats["failed"] += 1
            continue

        with db.connect() as conn:
            conn.execute(
                "UPDATE reports SET brief=?, brief_at=? WHERE id=?",
                (brief[:800], db.now_iso(), r["id"]),
            )
        stats["done"] += 1
        log.info("  [OK] %s (%d 字)", r["competitor"], len(brief))

    log.info("完成: %s", stats)
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("limit", nargs="?", type=int, default=100)
    ap.add_argument("--redo", action="store_true", help="强制重做已有的")
    a = ap.parse_args()
    print(run(limit=a.limit, redo=a.redo))
