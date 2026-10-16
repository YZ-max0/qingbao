"""官网变更监控适配器 —— 爬虫通路。

定位：竞品官网产品页/新闻页/招聘页，内容一变就产出一条 site_change 事件。
两种用法：
  1. 独立运行（本模块），轻量、无依赖；
  2. 若已部署 changedetection.io，可由它负责调度与通知，本模块作补充。
"""
from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup

from ..common import db
from ..common.fetcher import Fetcher
from ..common.scoring import match_competitor
from ..common.util import CONFIG_DIR, load_yaml, setup_logging

log = setup_logging("site_change")

# 只需关注正文变化，剥离导航/脚本等噪音节点
STRIP_SELECTORS = ["script", "style", "nav", "footer", "header", "noscript"]


def _normalize(html: str) -> tuple[str, str]:
    """返回 (标题, 归一化正文)。"""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for sel in STRIP_SELECTORS:
        for node in soup.select(sel):
            node.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return title, text


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _tiers(cfg: dict) -> dict:
    """competitors.yaml 的梯队定义嵌套在 tiers 下，这里统一取用。"""
    return cfg.get("tiers") or cfg


def sync_targets_from_config() -> int:
    """把 competitors.yaml 中的竞品官网写入 watch_targets。"""
    cfg = _tiers(load_yaml(CONFIG_DIR / "competitors.yaml"))
    rows = 0
    with db.connect() as conn:
        for tier in ("tier1", "tier2"):
            for comp in cfg.get(tier, []) or []:
                name = comp.get("name")
                if not name:
                    continue
                urls = comp.get("urls") or {}
                for kind, url in urls.items():
                    if not url:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO watch_targets (competitor, kind, url) "
                        "VALUES (?, ?, ?)",
                        (name, kind, url),
                    )
                    rows += 1
    log.info("同步监控目标 %d 条", rows)
    return rows


def check_targets(only_tier1: bool = True) -> dict[str, int]:
    """检查监控目标，内容变化则写入 site_change 事件。"""
    cfg = load_yaml(CONFIG_DIR / "competitors.yaml")
    comp_cfg = load_yaml(CONFIG_DIR / "sources.yaml")
    policy = comp_cfg.get("crawl_policy", {}) or {}
    fetcher = Fetcher(
        delay_range=tuple(policy.get("delay_seconds", [2, 5])),
        respect_robots=bool(policy.get("respect_robots", True)),
    )

    tier_filter = {"tier1"} if only_tier1 else {"tier1", "tier2"}
    stats = {"fetched": 0, "inserted": 0, "skipped": 0, "changed": 0}

    with db.connect() as conn:
        run_id = db.start_run(conn, "site_change")
        targets = conn.execute(
            "SELECT * FROM watch_targets WHERE enabled=1"
        ).fetchall()

        for t in targets:
            if t["competitor"] not in _names_for(tier_filter, _tiers(cfg)):
                continue
            html = fetcher.stream(t["url"])
            stats["fetched"] += 1
            if html is None:
                stats["skipped"] += 1
                continue

            text_html = html.decode("utf-8", errors="replace")
            title, text = _normalize(text_html)
            digest = _digest(text)

            if t["last_hash"] == digest:
                conn.execute(
                    "UPDATE watch_targets SET last_checked=? WHERE id=?",
                    (db.now_iso(), t["id"]),
                )
                stats["skipped"] += 1
                continue

            is_first = t["last_hash"] is None
            conn.execute(
                "UPDATE watch_targets SET last_hash=?, last_checked=? WHERE id=?",
                (digest, db.now_iso(), t["id"]),
            )
            # 首次抓取只建立基线，不产事件，避免刷屏
            if is_first:
                log.info("建立基线: %s %s", t["competitor"], t["kind"])
                continue

            entity, tier = match_competitor(t["competitor"])
            ok = db.insert_event(
                conn,
                source_type="scrape",
                source_name=f"{t['competitor']}官网",
                channel="site_change",
                entity=t["competitor"],
                entity_tier=tier or ("tier1" if only_tier1 else "tier2"),
                title=f"[官网变更] {t['competitor']} {t['kind']}: {title[:80]}",
                summary=f"页面内容发生变化，请人工核对。URL: {t['url']}",
                url=t["url"],
                relevance=0.8,
                tags=["官网变更", t["kind"]],
            )
            stats["changed"] += 1
            stats["inserted" if ok else "skipped"] += 1

        db.finish_run(
            conn, run_id, stats["fetched"], stats["inserted"], stats["skipped"]
        )
    log.info("官网变更完成: %s", stats)
    return stats


def _names_for(tiers: set[str], cfg: dict) -> set[str]:
    names: set[str] = set()
    for tier in tiers:
        for comp in cfg.get(tier, []) or []:
            names.add(comp.get("name"))
    return names
