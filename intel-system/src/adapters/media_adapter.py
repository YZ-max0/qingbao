"""行业媒体适配器 —— 覆盖「新品发布 / 融资 / 大单」三条线。

实测发现（2026-10）：行业媒体的报道本身就是融资与订单的信号源，
例如中国机器人网首页即出现「完成超亿元A+轮融资」「2000台大单敲定」
「携12款展品亮相工博会」，因此本适配器不仅抓"新品"，还从标题中
判别融资/订单/展会事件，分别打标签。

可用源（实测 HTTP 200）：
    中国机器人网  robot-china.com   —— 链接形态 /news/YYYYMM/DD/NNNNN.html
    OFweek机器人  robot.ofweek.com  —— 链接形态 /YYYY-MM/ART-...html
不可用（已排除）：
    高工机器人 gg-robot.com     503
    物流技术与应用 logisticstech.com.cn  域名解析失败
    亿欧 iyiou.com              202 反爬
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..common import db
from ..common.scoring import cfg as tender_cfg
from ..common.scoring import keyword_hits, match_competitor, policy_cfg
from ..common.util import USER_AGENT, setup_logging

log = setup_logging("media")

SITES: list[dict] = [
    {
        "name": "中国机器人网",
        "url": "https://www.robot-china.com/",
        "pattern": r"/news/\d{6}/\d{2}/\d+\.html",
        "tags": ["行业媒体", "机器人"],
    },
    {
        "name": "OFweek机器人",
        "url": "https://robot.ofweek.com/",
        "pattern": r"/\d{4}-\d{2}/ART-\d+-\d+-\d+\.html",
        "tags": ["行业媒体", "机器人"],
    },
    # ── 垂直媒体：专做移动机器人/仓储物流，命中率显著高于综合媒体 ──
    {
        "name": "AGV-AMR网",
        "url": "http://www.agv-amr.com/",
        "pattern": r"/news/show\.php\?itemid=\d+",
        "tags": ["行业媒体", "移动机器人", "垂直"],
    },
    {
        "name": "新战略机器人",
        "url": "http://xzlrobot.com/",
        # 该站文章是相对链接（c15129.html / cs-xxx.html），
        # urljoin 会解析为站根路径，故匹配规则不能带前导斜杠。
        "pattern": r"(?:^|/)c\d+\.html$|/cs-[\w-]+\.html$",
        "tags": ["行业媒体", "移动机器人", "垂直"],
    },
]

# 事件判别：媒体标题里的关键词 → 标签
# 注意：媒体稿的用词与政采公告不同。"中标"在媒体语境下指厂商拿了单，
# 但为避免把政策/综述类稿件误判为订单，这里要求同时出现商业信号词。
EVENT_RULES: list[tuple[tuple[str, ...], str]] = [
    (("融资", "天使轮", "A轮", "B轮", "C轮", "Pre-IPO", "亿元", "IPO", "敲钟上市",
      "过会", "战略投资", "增资"), "融资动态"),
    (("大单", "订单", "签约", "交付", "批量", "万台", "千台", "中标", "斩获",
      "拿下", "中标金额"), "订单信号"),
    (("发布", "新品", "推出", "亮相", "首发", "展出", "工博会", "展会", "上市发布"), "新品发布"),
]


def _detect_events(title: str) -> list[str]:
    """判别稿件属于哪类事件。

    需要排除"榜单/白皮书/评选"类稿件：它们常含"发布"字样（"榜单隆重发布"），
    但其实不是厂商发新品，误判会污染「新品发布」信号。
    """
    # 非事件型稿件：榜单、排行、报告、白皮书、评选、指数
    if any(w in title for w in (
        "榜单", "排行", "TOP", "Top", "top", "白皮书", "研究报告", "蓝皮书",
        "评选", "指数", "报告显示", "综合实力200强", "100强", "50强",
    )):
        return []

    out = []
    for words, tag in EVENT_RULES:
        if any(w in title for w in words):
            out.append(tag)
    return out


def _relevant(title: str) -> float:
    """媒体稿件相关度：用政策词表的宽口径（制造业/物流/机器人）。"""
    pc = policy_cfg()
    tc = tender_cfg()
    t = title.lower()
    for noise in (pc.get("noise") or []) + (tc.get("noise") or []):
        if noise.lower() in t:
            return 0.0
    hits = [k for k in (pc.get("core") or []) if k.lower() in t]
    hits += [k for k in (tc.get("core") or []) if k.lower() in t]
    if not hits:
        return 0.0
    score = 0.6
    # 明确的事件型稿件价值更高
    if _detect_events(title):
        score += 0.2
    if match_competitor(title)[0]:
        score += 0.2
    return round(min(score, 1.0), 2)


def _parse_list(html: str, base: str, pattern: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    items: list[dict] = []
    for a in soup.find_all("a", href=True):
        title = re.sub(r"\s+", " ", a.get_text(strip=True))
        if len(title) < 12 or not re.search(pattern, a["href"]):
            continue
        full = urljoin(base, a["href"])
        if full in seen:
            continue
        seen.add(full)
        items.append({"title": title, "url": full})
    return items


def run() -> dict[str, int]:
    stats = {"fetched": 0, "inserted": 0, "skipped": 0}
    sess = requests.Session()
    # 不走系统代理：本机代理对部分国内站点会连接中断且时好时坏，
    # 实测 13 个采集源走代理 3 个失败、直连 0 个失败。
    sess.trust_env = False
    sess.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})

    with db.connect() as conn:
        run_id = db.start_run(conn, "industry_media")
        for site in SITES:
            try:
                resp = sess.get(site["url"], timeout=20, verify=False)
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "utf-8"
            except Exception as exc:  # noqa: BLE001
                log.warning("%s 抓取失败: %s", site["name"], exc)
                continue

            items = _parse_list(resp.text, site["url"], site["pattern"])
            log.info("%s -> %d 条", site["name"], len(items))

            for it in items:
                stats["fetched"] += 1
                rel = _relevant(it["title"])
                if rel <= 0:
                    stats["skipped"] += 1
                    continue

                events = _detect_events(it["title"])
                # 媒体稿件里的竞品名是"被提及方"，不排除任何主体
                entity, tier = match_competitor(it["title"])
                ok = db.insert_event(
                    conn,
                    source_type="scrape",
                    source_name=site["name"],
                    channel="media",
                    entity=entity,
                    entity_tier=tier,
                    title=it["title"],
                    summary="",
                    url=it["url"],
                    relevance=rel,
                    tags=sorted(set(
                        list(site["tags"]) + events + keyword_hits(it["title"], kind="policy")
                        + (["竞品命中"] if entity else [])
                    )),
                )
                stats["inserted" if ok else "skipped"] += 1

        db.finish_run(
            conn, run_id, stats["fetched"], stats["inserted"], stats["skipped"]
        )
    log.info("行业媒体完成: %s", stats)
    return stats
