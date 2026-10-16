"""正文抓取 —— 为情报条目补上文章全文，供后续 LLM 提炼。

为什么需要：列表摘要只有标题时提炼不出内容。多数站点标题即一句话，
真正有价值的细节（谁、做了什么、数字、结论）都在正文里。

各站正文容器经实测确认（2026-10）：
    中国机器人网   div.content   或 div.m3（含标题头，优先 content）
    OFweek机器人   div.artical-content
    AGV-AMR网      div.content
    新战略机器人    div.articleWrapper
    中标公告(ccgp) 表格型页面，由 award_adapter 处理，这里不重复抓
"""
from __future__ import annotations

import re
import time

import requests
from bs4 import BeautifulSoup

from ..common import db
from ..common.util import USER_AGENT, setup_logging

log = setup_logging("article")

# 站点 → 正文容器候选（按优先级）
SELECTORS: dict[str, list[str]] = {
    "中国机器人网": ["div.content", "div.m3"],
    "OFweek机器人": ["div.artical-content", "div.article-content"],
    "AGV-AMR网": ["div.content", "div#content"],
    "新战略机器人": ["div.articleWrapper", "div.content"],
}
DEFAULT_SELECTORS = ["div.article-content", "div.content", "article", "div.artical-content"]

# 正文里要剔除的噪音块
STRIP = ["script", "style", "nav", "footer", "header", "noscript", "iframe",
         "form", "aside", ".share", ".ad", ".adv", ".related", ".comment"]

MIN_LEN = 120          # 少于这个字数视为抓取失败
MAX_LEN = 8000         # 截断上限，控制存储与 LLM 成本
FETCH_INTERVAL = 0.8   # 条与条之间的礼貌延迟（秒）


def _clean(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "")
    # 去掉常见的站点尾巴与交互词
    t = re.sub(r"(登录|注册|收藏|取消收藏|订阅|纠错|扫码关注|微信|微博|QQ群)\s*", "", t)
    t = re.sub(r"(上一篇|下一篇)[:：].*$", "", t)

    # 头部工具栏噪音：形如「打印 字号: 小 中 大 分享 34K」。
    # 各站顺序/有无不一，用"从开头连续吃掉这些词"的方式处理更稳。
    t = re.sub(
        r"^[\s　]*(?:打印|字号|分享|收藏|评论|纠错|大|中|小|[:：]|[\d.]+[Kk万亿]?|\s)*",
        "", t,
    )
    t = re.sub(r"核心提示[:：]\s*", "", t)
    # 正文常以标题重复开头，去掉一次
    return t.strip(" 　")


def extract(html: str, source_name: str) -> str:
    """从 HTML 中抽取正文纯文本。"""
    soup = BeautifulSoup(html, "lxml")
    for sel in STRIP:
        for node in soup.select(sel):
            node.decompose()

    for sel in SELECTORS.get(source_name, []) + DEFAULT_SELECTORS:
        el = soup.select_one(sel)
        if not el:
            continue
        txt = _clean(el.get_text(" ", strip=True))
        if len(txt) >= MIN_LEN:
            return txt[:MAX_LEN]
    # 兜底：整页文本（可用性差，但好过没有）
    txt = _clean(soup.get_text(" ", strip=True))
    return txt[:MAX_LEN] if len(txt) >= MIN_LEN else ""


def fetch_bodies(limit: int = 60, channels: tuple[str, ...] = ("media",)) -> dict[str, int]:
    """为尚无正文的条目抓取正文。

    body IS NULL      -> 尚未尝试
    body = ''         -> 抓过但没有可用正文（配合 body_tries 限制重试次数）
    body 有内容       -> 成功
    """
    ph = ",".join("?" * len(channels))
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT id, source_name, url FROM events
                WHERE channel IN ({ph})
                  AND url IS NOT NULL AND url <> ''
                  AND body IS NULL
                  AND COALESCE(body_tries,0) < 3
                ORDER BY COALESCE(NULLIF(published_at,''), captured_at) DESC
                LIMIT ?""",
            list(channels) + [limit],
        ).fetchall()

    todo = [dict(r) for r in rows]
    if not todo:
        log.info("没有待抓正文的条目")
        return {"fetched": 0, "failed": 0, "skipped": 0}

    log.info("待抓正文: %d 条", len(todo))
    sess = requests.Session()
    # 不走系统代理：本机代理对部分国内站点会连接中断且时好时坏，
    # 实测 13 个采集源走代理 3 个失败、直连 0 个失败。
    sess.trust_env = False
    sess.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})

    stats = {"fetched": 0, "failed": 0, "skipped": 0}
    with db.connect() as conn:
        run_id = db.start_run(conn, "article_body")
        for n, it in enumerate(todo):
            if n:
                time.sleep(FETCH_INTERVAL)   # 礼貌延迟，避免高频请求对端
            try:
                resp = sess.get(it["url"], timeout=20, verify=False)
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "utf-8"
            except Exception as exc:  # noqa: BLE001
                log.warning("正文抓取失败 %s: %s", it["url"][:60], str(exc)[:70])
                conn.execute(
                    "UPDATE events SET body_tries=COALESCE(body_tries,0)+1 WHERE id=?",
                    (it["id"],),
                )
                stats["failed"] += 1
                continue

            body = extract(resp.text, it["source_name"])
            if len(body) < MIN_LEN:
                # 记录已尝试且无正文，避免每轮重复抓取
                conn.execute(
                    "UPDATE events SET body='', body_tries=COALESCE(body_tries,0)+1,"
                    " updated_at=? WHERE id=?",
                    (db.now_iso(), it["id"]),
                )
                stats["skipped"] += 1
                continue

            conn.execute(
                "UPDATE events SET body=?, body_tries=COALESCE(body_tries,0)+1,"
                " updated_at=? WHERE id=?",
                (body, db.now_iso(), it["id"]),
            )
            stats["fetched"] += 1

        db.finish_run(conn, run_id, len(todo), stats["fetched"], stats["skipped"])
    log.info("正文抓取完成: %s", stats)
    return stats
