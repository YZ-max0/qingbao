"""把自建情报系统的近期动态导出为 competitor-intel 的分析上下文。

为什么需要：
    competitor-intel 靠 Tavily 搜索获取信息，但它**不知道我们每天直采到的
    一手数据**（政采中标、上市公司公告、垂直媒体动态）。缺了这些，它的
    SWOT 只能基于搜索引擎能看到的东西，结论会偏浅。

    上游已预留 prior_context 参数（注入 researcher 的 prompt），本模块
    负责把自建库里的相关记录汇总成该参数所需的一段文本。

设计要点：
    - 只导出**与目标竞品相关**的记录（entity 匹配 + 标题/摘要提及）
    - 按时间倒序，控制条数，避免撑爆 prompt
    - 保留日期与来源，让模型能引用
    - 标注"这是我方内部采集的事实"，与搜索结果区分开
"""
from __future__ import annotations

import json
import re

from ..common import db
from ..common.util import setup_logging

log = setup_logging("context_export")

# 上游把 prior_context 直接拼进 prompt，过长会挤占搜索结果的篇幅
MAX_ITEMS = 40
MAX_CHARS = 6000


def _tags(row: dict) -> list[str]:
    try:
        return json.loads(row.get("tags") or "[]")
    except json.JSONDecodeError:
        return []


def export_context(competitor: str, days: int = 180) -> str:
    """汇总某竞品的近期动态，返回可直接作为 prior_context 的文本。

    返回空字符串表示没有相关记录（调用方应跳过注入）。
    """
    if not competitor:
        return ""

    like = f"%{competitor}%"
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT entity, channel, source_name, title, summary,
                   llm_digest, llm_impact, published_at, captured_at, tags
            FROM events
            WHERE (entity = ? OR title LIKE ? OR summary LIKE ?)
              AND COALESCE(NULLIF(published_at,''), captured_at) >=
                  datetime('now', ?)
              AND channel <> 'analysis'
            ORDER BY COALESCE(NULLIF(published_at,''), captured_at) DESC
            LIMIT ?
            """,
            (competitor, like, like, f"-{int(days)} days", MAX_ITEMS),
        ).fetchall()

    items = [dict(r) for r in rows]
    if not items:
        return ""

    lines: list[str] = []
    for it in items:
        when = (it.get("published_at") or it.get("captured_at") or "")[:10]
        # 优先用 LLM 提炼的一句话，其次规则摘要，最后标题
        what = (it.get("llm_digest") or "").strip()
        if not what:
            what = (it.get("title") or "").strip()
        src = it.get("source_name") or ""
        chan = it.get("channel") or ""
        tags = [t for t in _tags(it)
                if t in ("重大变更", "融资动态", "订单信号", "新品发布",
                         "竞品命中", "中标", "招标", "官网变更")]
        tag_str = f" [{'/'.join(tags)}]" if tags else ""

        line = f"- {when}｜{chan}｜{src}{tag_str}：{what}"
        # 附带影响判断，帮助模型理解我方视角
        imp = (it.get("llm_impact") or "").strip()
        if imp:
            line += f"（我方判断：{imp}）"
        lines.append(line)

    header = (
        f"【我方内部采集的事实（{competitor}）】\n"
        f"以下条目来自我司自建情报系统，均为公开渠道直采的一手信息"
        f"（政府采购网、上市公司公告、行业垂直媒体等），"
        f"覆盖近 {days} 天，共 {len(lines)} 条，按时间倒序。\n"
        f"请把这些视为**已知事实**，分析时优先采用并与搜索结果相互印证；"
        f"重点关注其中反映出的**近期变化与最新动向**。\n"
    )
    text = header + "\n".join(lines)

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n…（其余记录已省略）"

    log.info("为 %s 导出上下文 %d 条（%d 字符）",
             competitor, len(lines), len(text))
    return text


def export_contexts(competitors: list[str], days: int = 180) -> dict[str, str]:
    """批量导出，返回 {竞品: 上下文文本}，仅含非空项。

    对应上游 run_batch(prior_contexts=...) 的参数形态。
    """
    out: dict[str, str] = {}
    for c in competitors:
        ctx = export_context(c, days=days)
        if ctx:
            out[c] = ctx
    return out


def stats(competitor: str) -> dict:
    """统计某竞品在自建库里的记录数（用于判断值不值得注入）。"""
    like = f"%{competitor}%"
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE (entity = ? OR title LIKE ? OR summary LIKE ?) "
            "AND channel <> 'analysis'",
            (competitor, like, like),
        ).fetchone()[0]
    return {"competitor": competitor, "events": n}
