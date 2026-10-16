"""融资/股本变动适配器 —— 用股本变动推断融资与股权动作（API 通路）。

为什么用这个而不是企查查/IT桔子：
    第三方融资数据库（企查查/天眼查/IT桔子）需付费 API 且多不允许二次分发，
    而「股本变动」是交易所公开披露数据，免费、结构化、可直接引用。

实测（2026-10）ak.stock_share_change_cninfo 返回的「变动原因」包含：
    定期报告 / 增发新股上市,股权激励 / 送转股 / 回购 等
其中「增发新股」= 真实融资动作，「股权激励」= 核心团队绑定信号。

局限（必须说明）：
    只能覆盖上市公司。非上市竞品（海柔创新、快仓、极智数通、仙工智能）
    没有披露义务，仍需依赖行业媒体报道（见 media_adapter）。
"""
from __future__ import annotations

import warnings

from ..common import db
from ..common.util import CONFIG_DIR, load_yaml, setup_logging

log = setup_logging("finance")

# 变动原因 → 情报价值判定
REASON_RULES: list[tuple[tuple[str, ...], str, float]] = [
    (("增发", "募集", "发行"), "融资动态", 0.85),
    (("股权激励", "限制性股票", "激励"), "融资动态", 0.7),
    (("回购", "注销"), "股本变动", 0.6),
    (("送转", "转增", "派股"), "股本变动", 0.5),
    (("定期报告",), "股本变动", 0.3),
]


def _targets() -> list[dict]:
    cfg = load_yaml(CONFIG_DIR / "competitors.yaml")
    tiers = cfg.get("tiers") or cfg
    out = []
    for tier in ("tier1", "tier2"):
        for comp in tiers.get(tier, []) or []:
            code = comp.get("stock_code")
            if code:
                out.append({"name": comp["name"], "code": str(code), "tier": tier})
    return out


def _classify(reason: str) -> tuple[str, float]:
    for words, tag, score in REASON_RULES:
        if any(w in reason for w in words):
            return tag, score
    return "股本变动", 0.5


def run(begin_date: str = "2024-01-01") -> dict[str, int]:
    warnings.filterwarnings("ignore")
    try:
        import akshare as ak
    except ImportError:
        log.error("未安装 akshare，跳过。安装: pip install akshare")
        return {"fetched": 0, "inserted": 0, "skipped": 0}

    from datetime import datetime

    end = datetime.now().strftime("%Y%m%d")
    start = begin_date.replace("-", "")

    stats = {"fetched": 0, "inserted": 0, "skipped": 0}
    with db.connect() as conn:
        run_id = db.start_run(conn, "finance_share_change")
        for t in _targets():
            try:
                df = ak.stock_share_change_cninfo(
                    symbol=t["code"], start_date=start, end_date=end
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("%s(%s) 股本变动获取失败: %s", t["name"], t["code"], exc)
                continue

            if df is None or df.empty:
                log.info("%s(%s) 无股本变动记录", t["name"], t["code"])
                continue

            log.info("%s(%s) -> %d 条股本变动", t["name"], t["code"], len(df))
            for _, row in df.iterrows():
                stats["fetched"] += 1
                reason = str(row.get("变动原因") or "").strip()
                if not reason:
                    stats["skipped"] += 1
                    continue

                # 无实质变化的记录不产事件，避免噪音
                if reason in ("定期报告",) and float(row.get("总股本") or 0) <= 0:
                    stats["skipped"] += 1
                    continue

                change_date = str(row.get("变动日期") or "")
                notice_date = str(row.get("公告日期") or "")
                total = row.get("总股本")
                tag, score = _classify(reason)

                title = f"{t['name']} 股本变动：{reason}"
                summary = (
                    f"变动日期: {change_date} | 公告日期: {notice_date} | "
                    f"变动原因: {reason}"
                    + (f" | 总股本: {total}" if total is not None else "")
                )

                tags = ["股本变动", tag]
                if tag == "融资动态":
                    tags.append("融资信号")
                ok = db.insert_event(
                    conn,
                    source_type="api",
                    source_name=f"{t['name']}股本变动",
                    channel="finance",
                    entity=t["name"],
                    entity_tier=t["tier"],
                    title=title,
                    summary=summary,
                    url="",
                    published_at=notice_date or change_date,
                    relevance=score,
                    tags=sorted(set(tags)),
                )
                stats["inserted" if ok else "skipped"] += 1

        db.finish_run(
            conn, run_id, stats["fetched"], stats["inserted"], stats["skipped"]
        )
    log.info("股本变动完成: %s", stats)
    return stats
