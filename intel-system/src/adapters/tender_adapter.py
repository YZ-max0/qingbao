"""招标/中标公告适配器 —— 爬虫通路。

对应"对手举动最硬的信号"：中标公告。
先接中国政府采购网（ccgp）搜索接口，各省交易中心按同一接口扩展。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from ..common import db
from ..common.fetcher import Fetcher
from ..common.scoring import keyword_hits, match_competitor, score_relevance
from ..common.util import CONFIG_DIR, load_yaml, setup_logging

log = setup_logging("tender")

CCGP_SEARCH = "https://search.ccgp.gov.cn/bxsearch"
# 实测：HTTP 被拦；HTTPS 可用但存在 IP 级频率限制，必须极低频
CCGP_MIN_INTERVAL = 60.0  # 秒：关键词之间的最小间隔


def _cfg() -> dict:
    """返回拍平后的配置（core/weak/noise/scene 提升到顶层）。

    直接用 load_yaml 拿到的是嵌套结构（category_keywords.core），
    这里复用 scoring 的拍平逻辑，避免两处解析口径不一致。
    """
    from ..common.scoring import cfg as _flat_cfg

    return _flat_cfg()


def _search_terms(cfg: dict) -> list[str]:
    """检索词。

    实测：在关键词后追加"中标"可让结果几乎全是中标公告
    （政采搜索是全文匹配，标题含"中标/成交"的才会命中），
    比猜测 bidType 参数可靠得多，也让详情页解析有的放矢。
    """
    core = cfg.get("core", []) or []
    preferred = ["仓储机器人", "AGV", "无人叉车", "智能仓储", "移动机器人"]
    bases = [k for k in preferred if k in core] or core[:5]
    terms = list(bases)
    # 追加中标检索：专抓"谁赢了"，供奖项详情页解析使用
    terms += [f"{b} 中标" for b in bases[:3]]
    return terms


def _norm_date(raw: str) -> str:
    """把 ccgp 的 '2026.10.13 14:33:38' 归一为 ISO '2026-10-13 14:33:38'。

    日期格式不统一会让跨源排序和按日聚合出错（'2026.06.09' 与 '2026-09-30'
    会被当成两个不同的键），所以在入库前统一。
    """
    s = (raw or "").strip()
    m = re.match(r"^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})(.*)$", s)
    if not m:
        return s
    y, mo, d, rest = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}{rest}"


def _parse_ccgp(html: str) -> list[dict]:
    """解析政府采购网搜索结果页。"""
    if "频繁访问" in html:
        log.warning(
            "被 ccgp 频率限制拦截（返回'频繁访问'页）。"
            "请降低频率或减少运行次数，见 config/sources.yaml 的 rate_limited 说明。"
        )
        return []

    soup = BeautifulSoup(html, "lxml")
    items: list[dict] = []

    for li in soup.select("ul.vT-srch-result-list-bid li"):
        a = li.find("a")
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a.get("href", "")
        span = li.find("span")
        # span 包住整个条目文本（含采购人/代理机构），不是纯日期。
        # 只取其中第一个日期时间片段，否则 published_at 会存进整段正文。
        span_text = re.sub(r"\s+", " ", span.get_text(" ", strip=True)) if span else ""
        m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})(?:\s+(\d{1,2}:\d{2}:\d{2}))?", span_text)
        published = ""
        if m:
            y, mo, d, tm = m.groups()
            published = f"{y}-{int(mo):02d}-{int(d):02d}" + (f" {tm}" if tm else "")
        text = re.sub(r"\s+", " ", li.get_text(" ", strip=True)).strip()
        items.append(
            {"title": title, "url": href, "published": published, "blob": text}
        )
    return items


def fetch_ccgp(fetcher: Fetcher, term: str, days: int = 30, page: int = 1) -> list[dict]:
    end = datetime.now()
    start = end - timedelta(days=days)
    params = {
        "searchtype": "1",
        "page_index": str(page),
        "bidSort": "0",
        "pinMu": "0",
        "bidType": "0",
        "dbselect": "bidx",
        "kw": term,
        "timeType": "6",
        "start_time": start.strftime("%Y:%m:%d"),
        "end_time": end.strftime("%Y:%m:%d"),
    }
    resp = fetcher.get(CCGP_SEARCH, params=params)
    if resp is None:
        return []
    resp.encoding = resp.apparent_encoding or "utf-8"
    return _parse_ccgp(resp.text)


def run(days: int = 30, pages: int = 2) -> dict[str, int]:
    cfg = _cfg()
    policy = cfg.get("crawl_policy", {}) or {}
    max_pages = int(policy.get("max_pages_per_run", 40))

    fetcher = Fetcher(
        delay_range=tuple(policy.get("delay_seconds", [2, 5])),
        respect_robots=bool(policy.get("respect_robots", True)),
    )

    stats = {"fetched": 0, "inserted": 0, "skipped": 0}
    requests_made = 0

    with db.connect() as conn:
        run_id = db.start_run(conn, "tender_ccgp")
        for term in _search_terms(cfg):
            if requests_made >= max_pages:
                log.info("达到单次运行页数上限 %d，停止", max_pages)
                break
            if requests_made > 0:
                log.info("ccgp 限频保护：等待 %.0f 秒", CCGP_MIN_INTERVAL)
                time.sleep(CCGP_MIN_INTERVAL)
            requests_made += 1
            # 分页：ccgp 单页约 20 条，多取几页提高召回
            items: list[dict] = []
            for page in range(1, pages + 1):
                if page > 1:
                    time.sleep(CCGP_MIN_INTERVAL)
                got = fetch_ccgp(fetcher, term, days=days, page=page)
                if not got:
                    break
                items.extend(got)
            log.info("关键词 [%s] -> %d 条", term, len(items))

            for it in items:
                stats["fetched"] += 1
                blob = f"{it['title']} {it['blob']}"
                relevance = score_relevance(it["title"], blob)
                if relevance < 0.5:
                    stats["skipped"] += 1
                    continue

                entity, tier = match_competitor(blob)
                tags = keyword_hits(blob)
                is_win = ("中标" in it["title"]) or ("成交" in it["title"])
                if is_win:
                    tags += ["中标", "订单信号"]
                if "招标" in it["title"]:
                    tags.append("招标")
                if entity:
                    tags.append("竞品命中")

                ok = db.insert_event(
                    conn,
                    source_type="scrape",
                    source_name="中国政府采购网",
                    channel="tender",
                    entity=entity,
                    entity_tier=tier,
                    title=it["title"],
                    summary=it["blob"][:800],
                    url=it["url"],
                    published_at=it["published"],
                    relevance=relevance,
                    tags=sorted(set(tags)),
                )
                stats["inserted" if ok else "skipped"] += 1

        db.finish_run(
            conn, run_id, stats["fetched"], stats["inserted"], stats["skipped"]
        )
    log.info("招标完成: %s", stats)
    return stats
