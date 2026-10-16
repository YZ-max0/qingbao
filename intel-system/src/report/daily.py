"""日报生成：本地汇总（无需 LLM key），可选接入 LLM 写摘要。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from ..common import db
from ..common.util import DATA_DIR, ensure_dirs, setup_logging

log = setup_logging("report")

CHANNEL_LABEL = {
    "tender": "招投标 / 中标",
    "policy": "政策",
    "media": "行业媒体",
    "site_change": "官网变更",
    "announcement": "上市公告",
    "finance": "融资 / 股本变动",
    "analysis": "深度分析报告",
}

# 需要置顶的高价值标签：这些是"对手真的动手了"的信号
PRIORITY_TAGS = [
    ("重大变更", "🚨 重大变更（控制权/收购/重组）"),
    ("融资动态", "💵 融资动态"),
    ("订单信号", "💰 订单信号（中标/重大合同）"),
    ("新品发布", "🆕 新品发布"),
    ("深度分析", "📊 深度分析报告"),
    ("竞品命中", "🎯 竞品直接命中"),
]


def collect(days: int = 1, min_relevance: float = 0.4) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE captured_at >= ? AND relevance >= ?
            ORDER BY relevance DESC, captured_at DESC
            """,
            (since, min_relevance),
        ).fetchall()
    return [dict(r) for r in rows]


def cluster_events(events: list[dict]) -> list[list[dict]]:
    """把"同一事件的多次进展"折叠成一组。

    上市公司会对同一事项按月发布进展公告，标题高度相似。若不折叠，
    置顶区会被同一件事刷屏，真正的新信号反而看不见。
    """
    import re

    groups: dict[str, list[dict]] = {}
    for e in events:
        # 归一化标题中的日期、序号、"进展/补充"等修饰，得到事件指纹
        key = re.sub(r"[（(].*?[)）]|\d+", "", e["title"])
        key = key.replace("进展", "").replace("补充", "").replace("第次", "")
        key = re.sub(r"\s+", "", key)[:40]
        groups.setdefault(f"{e.get('entity')}|{key}", []).append(e)

    out = []
    for items in groups.values():
        items.sort(key=lambda x: x.get("published_at") or "", reverse=True)
        out.append(items)
    out.sort(key=lambda g: g[0].get("published_at") or "", reverse=True)
    return out


def render_markdown(events: list[dict], days: int = 1) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 仓储 AMR 竞品情报日报 · {today}",
        "",
        f"> 覆盖近 {days} 天，共 {len(events)} 条高相关事件",
        "",
    ]

    # 高价值信号置顶：按标签优先级分组，避免淹没在长列表里
    shown: set[int] = set()
    for tag, heading in PRIORITY_TAGS:
        hits = [e for e in events if tag in (e.get("tags") or "")]
        if not hits:
            continue
        clusters = cluster_events(hits)
        lines += [f"## {heading}（{len(hits)} 条 / {len(clusters)} 个事件）", ""]
        for c in clusters:
            latest = c[0]
            shown.update(e["id"] for e in c)
            extra = f"  \n  _另有 {len(c) - 1} 条同期进展，最近 {c[-1].get('published_at') or ''}_" if len(c) > 1 else ""
            lines.append(
                f"- **[{latest['entity'] or '—'}]** {latest['title']}  \n"
                f"  `{latest['source_name']}` · 相关度 {latest['relevance']} · "
                f"{latest.get('published_at') or latest['captured_at']}  \n"
                f"  {latest.get('url') or ''}{extra}"
            )
        lines.append("")

    # 其余事件按频道分组（排除已置顶的）
    rest = [e for e in events if e["id"] not in shown]
    grouped: dict[str, list[dict]] = {}
    for e in rest:
        grouped.setdefault(e.get("channel") or "其他", []).append(e)

    for channel in ("analysis", "announcement", "finance", "tender", "media", "policy", "site_change"):
        items = grouped.get(channel)
        if not items:
            continue
        lines += [f"## {CHANNEL_LABEL.get(channel, channel)}（{len(items)}）", ""]
        for e in items[:20]:
            tags = e.get("tags") or "[]"
            lines.append(
                f"- [{e['title']}]({e.get('url') or '#'})  \n"
                f"  `{e['source_name']}` · 相关度 {e['relevance']} · {tags}"
            )
        if len(items) > 20:
            lines.append(f"- _…另有 {len(items) - 20} 条，详见数据库_")
        lines.append("")

    if not events:
        lines.append("_本周期无高相关事件。_")
    return "\n".join(lines)


def maybe_llm_summary(markdown: str) -> str | None:
    """可选：接入 LLM 生成一段人话摘要。未配置 key 时返回 None。"""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    try:
        import httpx

        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        resp = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是制造业竞品情报分析师。基于下方事件列表，"
                            "用中文写 5 条要点：对手在做什么、行业风向、值得警惕的信号。"
                            "只依据给定内容，不要编造。"
                        ),
                    },
                    {"role": "user", "content": markdown[:12000]},
                ],
                "temperature": 0.2,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM 摘要失败，跳过: %s", exc)
        return None


def run(days: int = 1, output: str | None = None) -> str:
    ensure_dirs()
    events = collect(days=days)
    md = render_markdown(events, days=days)

    summary = maybe_llm_summary(md)
    if summary:
        md += "\n\n---\n\n## 🤖 AI 要点\n\n" + summary + "\n"

    out = output or str(DATA_DIR / f"report_{datetime.now():%Y%m%d}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    log.info("日报已生成: %s (%d 条事件)", out, len(events))
    return out
