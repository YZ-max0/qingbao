"""上市公告适配器 —— API 通路（AkShare / 东方财富）。

这是整个系统最稳定的一环：结构化数据、有 schema、无风控。
实测（2026-10）：stock_individual_notice_report 可按代码取回历史公告，
今天国际(300532) 单次可返回 130 条。

重点关注的公告类型（高情报价值）：
  控制权/股权变更、重大合同、投资设立、业绩预告、股权激励
"""
from __future__ import annotations

import warnings

from ..common import db
from ..common.scoring import keyword_hits, match_competitor
from ..common.util import CONFIG_DIR, load_yaml, setup_logging

log = setup_logging("announcement")

# 高价值公告类型关键词：命中则抬高相关度
HIGH_VALUE = [
    "控制权", "收购", "重大合同", "中标", "签约", "投资设立", "对外投资",
    "业绩", "增资", "股权激励", "合并", "重组", "减持", "增持",
]


def _targets() -> list[dict]:
    cfg = load_yaml(CONFIG_DIR / "competitors.yaml")
    tiers = cfg.get("tiers") or cfg
    out: list[dict] = []
    for tier in ("tier1", "tier2"):
        for comp in tiers.get(tier, []) or []:
            code = comp.get("stock_code")
            if code:
                out.append(
                    {
                        "name": comp.get("name"),
                        "code": str(code),
                        "tier": tier,
                    }
                )
    return out


def _score(title: str, notice_type: str) -> float:
    """公告相关度：高价值类型加分；公告本身即高信号，基线较高。"""
    score = 0.5
    blob = f"{title} {notice_type}"
    if any(k in blob for k in HIGH_VALUE):
        score += 0.3
    if any(k in blob for k in ("中标", "重大合同", "签约")):
        score += 0.2
    return round(min(score, 1.0), 2)


def run(begin_date: str = "2026-01-01", end_date: str | None = None) -> dict[str, int]:
    warnings.filterwarnings("ignore")
    try:
        import akshare as ak
    except ImportError:
        log.error("未安装 akshare，跳过。安装: pip install akshare")
        return {"fetched": 0, "inserted": 0, "skipped": 0}

    from datetime import datetime

    end_date = end_date or datetime.now().strftime("%Y-%m-%d")

    stats = {"fetched": 0, "inserted": 0, "skipped": 0}
    with db.connect() as conn:
        run_id = db.start_run(conn, "listed_announcement")
        for t in _targets():
            try:
                df = ak.stock_individual_notice_report(
                    security=t["code"],
                    symbol="全部",
                    begin_date=begin_date,
                    end_date=end_date,
                )
            except Exception as exc:  # noqa: BLE001
                log.error("%s(%s) 公告获取失败: %s", t["name"], t["code"], exc)
                continue

            if df is None or df.empty:
                log.info("%s(%s) -> 0 条", t["name"], t["code"])
                continue

            log.info("%s(%s) -> %d 条公告", t["name"], t["code"], len(df))
            for _, row in df.iterrows():
                stats["fetched"] += 1
                title = str(row.get("公告标题") or "").strip()
                if not title:
                    stats["skipped"] += 1
                    continue

                notice_type = str(row.get("公告类型") or "")
                published = str(row.get("公告日期") or "")
                url = str(row.get("网址") or "")
                relevance = _score(title, notice_type)

                tags = ["上市公告", notice_type]
                if any(k in f"{title}{notice_type}" for k in ("控制权", "收购", "重组")):
                    tags.append("重大变更")
                if any(k in f"{title}{notice_type}" for k in ("中标", "重大合同", "签约")):
                    tags.append("订单信号")
                tags += keyword_hits(title)

                # exclude 本公司：公告标题必然写自己的名字，那不是"竞品命中"
                other, other_tier = match_competitor(title, exclude=t["name"])
                if other:
                    tags.append("竞品命中")

                ok = db.insert_event(
                    conn,
                    source_type="api",
                    source_name=f"{t['name']}上市公告",
                    channel="announcement",
                    entity=t["name"],
                    entity_tier=t["tier"],
                    title=title,
                    summary=f"公告类型: {notice_type}"
                    + (f" | 另提及: {other}" if other else ""),
                    url=url,
                    published_at=published,
                    relevance=relevance,
                    tags=sorted(set(tags)),
                )
                stats["inserted" if ok else "skipped"] += 1

        db.finish_run(
            conn, run_id, stats["fetched"], stats["inserted"], stats["skipped"]
        )
    log.info("上市公告完成: %s", stats)
    return stats
