"""把 competitor-intel 搜索到的来源回补进自建情报库。

为什么需要（实测驱动）：
    自建系统的固定源覆盖很窄 —— 45 个竞品里只有 10 个有数据，
    第一梯队核心的「海柔创新」在库里是 0 条（它未上市、不走政采、
    媒体提及也少）。而 competitor-intel 靠 Tavily 全网搜索，能覆盖到它们。

    因此需要反向回补，否则那 35 个竞品在看板上永远是空的。

数据形态：
    上游 researcher 的返回里已含 `sources: [{id, title, url}]`
    （见 agents/researcher.py 的 _build_numbered_sources），
    无需改动上游代码，直接从批量分析的返回值里取用。

写入策略：
    - channel = 'analysis_src'，与正式采集数据区分开
    - relevance 固定 0.5：这些是"搜索命中的链接"，未经我方筛选
    - 标签标注「搜索来源」，避免与直采数据混淆
    - 幂等：按 url 去重
"""
from __future__ import annotations

import json

from ..common import db
from ..common.scoring import match_competitor
from ..common.util import setup_logging

log = setup_logging("source_import")

CHANNEL = "analysis_src"
RELEVANCE = 0.5


def import_sources(competitor: str, sources: list[dict],
                   limit: int = 40) -> dict[str, int]:
    """把某竞品的搜索来源写入自建库。

    sources: [{id, title, url}, ...]，来自 researcher 的返回值。

    注意统计口径（实测教训）：
        "插入 0 条" **不等于** "回补无效"。对已有大量直采数据的竞品
        （如今天国际 135 条），Tavily 搜到的 URL 多半早已入库，
        去重后自然为 0 —— 这恰恰说明我们的覆盖是够的。

        因此额外统计 `new_coverage`：该竞品在回补前是否**完全没有数据**。
        这个指标才反映"融合是否补上了缺口"。
    """
    stats = {"seen": 0, "inserted": 0, "skipped": 0, "invalid": 0,
             "new_coverage": 0}

    if not sources:
        return stats

    # 回补前：该竞品在库里（除本频道外）是否已有数据
    with db.connect() as conn:
        like = f"%{competitor}%"
        before = conn.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE (entity = ? OR title LIKE ?) "
            "AND channel NOT IN ('analysis', 'analysis_src')",
            (competitor, like),
        ).fetchone()[0]

    with db.connect() as conn:
        run_id = db.start_run(conn, "import_analysis_sources")
        for s in sources[:limit]:
            stats["seen"] += 1
            url = (s.get("url") or "").strip()
            title = (s.get("title") or "").strip()
            if not url or not title:
                stats["invalid"] += 1
                continue

            # 标题里若出现别的竞品，归到那个竞品名下更准确
            entity, tier = match_competitor(title)
            if not entity:
                entity, tier = competitor, None

            ok = db.insert_event(
                conn,
                source_type="analysis",
                source_name=f"{competitor}·搜索来源",
                channel=CHANNEL,
                entity=entity,
                entity_tier=tier,
                title=title,
                summary=f"深度分析时检索到的来源（{competitor}），"
                        f"未经人工核验，仅供追溯",
                url=url,
                relevance=RELEVANCE,
                tags=["搜索来源", "深度分析"],
            )
            stats["inserted" if ok else "skipped"] += 1

        db.finish_run(conn, run_id, stats["seen"],
                      stats["inserted"], stats["skipped"])

    # 回补后：判断是否从"零覆盖"变为"有覆盖"
    if before == 0 and stats["inserted"] > 0:
        stats["new_coverage"] = 1

    log.info("%s 回补: 新增 %d 条 / 重复 %d 条 / %s",
             competitor, stats["inserted"], stats["skipped"],
             "★ 填补空白竞品" if stats["new_coverage"] else "（原已有数据）")
    return stats


def import_from_result(competitor: str, result: dict) -> dict[str, int]:
    """从 orchestrator 的完整返回里取 sources 并回补。"""
    srcs = (result.get("research") or {}).get("sources") or result.get("sources") or []
    return import_sources(competitor, srcs)


def run() -> dict[str, int]:
    """占位：真正的回补在批量分析时逐条进行（见 run_batch_analysis.py）。"""
    log.info("本模块由 run_batch_analysis.py 在分析后自动调用")
    return {"inserted": 0}
