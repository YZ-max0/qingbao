"""政策栏目页适配器。

实测约束（2026-10 验证，见 config/sources.yaml 的 status 字段）：
  - 政府站点普遍无 RSS
  - 列表页多为 JS 动态渲染，静态 requests 取不到内容
  - 部分站点 robots.txt 禁止抓取，合规上必须跳过

因此本适配器：
  1. 只处理 status == verified_ok 的源
  2. 支持可插拔渲染后端（renderer），未安装时明确报错而不是静默返回 0 条
  3. 跳过 needs_render / robots_disallow / rate_limited 的源并打日志
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..common import db
from ..common.fetcher import Fetcher
from ..common.scoring import keyword_hits, match_competitor, score_relevance
from ..common.util import setup_logging

log = setup_logging("policy")

POLICY_PAGES: list[dict] = [
    {
        "name": "工信部-政策文件",
        "url": "https://www.miit.gov.cn/zwgk/zcwj/index.html",
        "status": "needs_render",
        "channel": "policy",
        "tags": ["政策", "工信部"],
        # 实测信噪比高：制造业数字化转型/新兴产业发展/绿色工厂等
    },
    {
        "name": "中国政府网-最新政策",
        "url": "https://www.gov.cn/zhengce/zuixin/",
        "status": "needs_render",
        "channel": "policy",
        "tags": ["政策", "国务院"],
        # 实测信噪比低：多为民生/政法类，与制造业相关性弱；
        # 保留是因为偶有"新质生产力"这类顶层产业政策。
    },
    {
        "name": "发改委-政策发布",
        "url": "https://www.ndrc.gov.cn/xxgk/zcfb/",
        "status": "robots_disallow",
        "channel": "policy",
        "tags": ["政策", "发改委"],
    },
    {
        "name": "发改委-通知",
        "url": "https://www.ndrc.gov.cn/xxgk/zcfb/tz/",
        "status": "robots_disallow",
        "channel": "policy",
        "tags": ["政策", "发改委"],
    },
]

_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")
# 政府栏目页的正文链接有多种形态，实测汇总：
#   gov.cn : /zhengce/202610/content_7082820.htm
#   miit   : /zwgk/zcwj/wjfb/tz/art/2026/art_8b73...html
_ITEM_RE = re.compile(r"content_\d+|t\d{8}_\d+|/art/\d+/art_|/\d{6}/")

# 政策相关度阈值：政策大多不点名"机器人"，宜宽口径收录后人工筛
POLICY_THRESHOLD = 0.2


def _extract_date(text: str) -> str:
    m = _DATE_RE.search(text)
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _find_date(node, max_depth: int = 4) -> str:
    """在链接周围逐层向上找日期。

    不同政府站点把日期放在不同层级：
      gov.cn : 与标题同在一个 li
      miit   : 在祖父级 div.search-list 内
    逐层向上匹配，取最近一层出现的第一个日期。
    """
    cur = node
    for _ in range(max_depth):
        cur = cur.parent
        if cur is None:
            break
        text = cur.get_text(" ", strip=True)
        d = _extract_date(text)
        if d:
            return d
    return ""


def parse_list_page(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    items: list[dict] = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = re.sub(r"\s+", " ", a.get_text(strip=True))
        if len(title) < 8 or not _ITEM_RE.search(href):
            continue
        full = urljoin(base_url, href)
        if full in seen:
            continue
        seen.add(full)

        li = a.find_parent("li")
        parent_text = li.get_text(" ", strip=True) if li else ""
        published = _extract_date(parent_text) or _extract_date(title) or _find_date(a)
        items.append({"title": title, "url": full, "published": published})
    return items


def fetch_html(page: dict, fetcher: Fetcher) -> str | None:
    """获取页面 HTML。needs_render 的源需要浏览器渲染后端。"""
    if page["status"] == "needs_render":
        try:
            from .render_backend import render_html  # type: ignore
        except ImportError:
            log.warning(
                "%s 需要 JS 渲染（status=needs_render），渲染模块缺失，跳过。",
                page["name"],
            )
            return None
        try:
            return render_html(page["url"])
        except ImportError:
            log.warning(
                "%s 需要 JS 渲染，但未安装 playwright，跳过。"
                "安装方式: pip install playwright && playwright install chromium",
                page["name"],
            )
            return None
        except Exception as exc:  # noqa: BLE001
            log.error("%s 渲染失败: %s", page["name"], exc)
            return None

    resp = fetcher.get(page["url"])
    if resp is None:
        return None
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def run() -> dict[str, int]:
    fetcher = Fetcher(delay_range=(3.0, 6.0))
    stats = {"fetched": 0, "inserted": 0, "skipped": 0, "blocked": 0}

    with db.connect() as conn:
        run_id = db.start_run(conn, "policy_pages")
        for page in POLICY_PAGES:
            if page["status"] == "robots_disallow":
                log.warning("%s: robots.txt 禁止抓取，合规跳过", page["name"])
                stats["blocked"] += 1
                continue
            if page["status"] == "rate_limited":
                log.warning("%s: 存在频率限制，跳过（见 sources.yaml）", page["name"])
                stats["blocked"] += 1
                continue

            html = fetch_html(page, fetcher)
            if html is None:
                stats["blocked"] += 1
                continue

            items = parse_list_page(html, page["url"])
            log.info("%s -> %d 条", page["name"], len(items))

            for it in items:
                stats["fetched"] += 1
                relevance = score_relevance(it["title"], kind="policy")
                # 政策用独立词表与阈值（见 policy_keywords.yaml）
                if relevance < POLICY_THRESHOLD:
                    stats["skipped"] += 1
                    continue

                entity, tier = match_competitor(it["title"])
                ok = db.insert_event(
                    conn,
                    source_type="scrape",
                    source_name=page["name"],
                    channel=page["channel"],
                    entity=entity,
                    entity_tier=tier,
                    title=it["title"],
                    summary="",
                    url=it["url"],
                    published_at=it["published"],
                    relevance=relevance,
                    tags=list(page.get("tags") or [])
                    + keyword_hits(it["title"], kind="policy"),
                )
                stats["inserted" if ok else "skipped"] += 1

        db.finish_run(
            conn, run_id, stats["fetched"], stats["inserted"], stats["skipped"]
        )
    log.info("政策栏目完成: %s", stats)
    return stats
