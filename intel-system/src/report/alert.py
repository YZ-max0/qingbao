"""告警模块：把新出现的高价值信号推到邮件 / 钉钉 / 通用 Webhook。

设计要点：
  1. 只推"新的"信号 —— alerts_sent 表按 (event_id, channel) 去重，
     同一事件不会每天重复打扰。
  2. 凭据全部走环境变量，配置文件只存变量名，避免密钥入库。
  3. 任一渠道失败不影响其他渠道，失败原因写入 alerts_sent.error。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import smtplib
import time
import urllib.parse
from email.header import Header
from email.mime.text import MIMEText

import httpx

from ..common import db
from ..common.util import CONFIG_DIR, ROOT, load_yaml, setup_logging

log = setup_logging("alert")


def _cfg() -> dict:
    return load_yaml(CONFIG_DIR / "alerts.yaml")


def collect_new(conn, cfg: dict, channel: str) -> list[dict]:
    """取出尚未在该渠道推送过、且命中触发条件的事件。

    会对"同一事项的多次进展"折叠：上市公司按月发进展公告，若不折叠，
    10 条同类公告会占满推送，真正的新信号被淹没。折叠后只推送最新一条，
    并标注同期进展数量。

    注意：折叠在 LIMIT 之前完成。若先 LIMIT 再折叠，折叠掉的名额会被
    下一轮重新取出，表现为"同一批信号反复推送"。
    """
    t = cfg.get("triggers", {}) or {}
    tags = t.get("tags") or []
    min_rel = float(t.get("min_relevance", 0.7))
    limit = int(t.get("max_items", 20))

    rows = conn.execute(
        """
        SELECT e.* FROM events e
        LEFT JOIN alerts_sent a
          ON a.event_id = e.id AND a.channel = ? AND a.ok = 1
        WHERE a.id IS NULL
          AND e.relevance >= ?
        ORDER BY e.relevance DESC, e.published_at DESC
        """,
        (channel, min_rel),
    ).fetchall()

    matched = []
    for r in rows:
        e = dict(r)
        e_tags = e.get("tags") or "[]"
        if tags and not any(tag in e_tags for tag in tags):
            continue
        matched.append(e)

    return cluster(matched)[:limit]


def absorb_backlog(conn, cfg: dict, channel: str, pending: int) -> int:
    """首次运行时吸收历史存量，避免连续多轮把群刷屏。

    库里已有大量历史事件时（例如刚接入系统），若逐轮补推，会推很多轮
    且内容早已过期。这里在积压超过阈值时，把较早的那些直接标记为已发，
    只保留最新一批真正推送。
    """
    t = cfg.get("triggers", {}) or {}
    threshold = int(t.get("backlog_threshold", 0) or 0)
    if threshold <= 0 or pending <= threshold:
        return 0

    keep = int(t.get("max_items", 20))
    tags = t.get("tags") or []
    min_rel = float(t.get("min_relevance", 0.7))

    rows = conn.execute(
        """
        SELECT e.id, e.tags FROM events e
        LEFT JOIN alerts_sent a
          ON a.event_id = e.id AND a.channel = ? AND a.ok = 1
        WHERE a.id IS NULL AND e.relevance >= ?
        ORDER BY e.relevance DESC, e.published_at DESC
        """,
        (channel, min_rel),
    ).fetchall()

    matched = [dict(r) for r in rows
               if not tags or any(tag in (r["tags"] or "") for tag in tags)]
    stale = matched[keep:]
    for e in stale:
        mark_sent(conn, e["id"], channel, True, "backlog-absorbed")
    if stale:
        log.info("[%s] 历史存量 %d 条已跳过（阈值 %d），仅推送最新 %d 条",
                 channel, len(stale), threshold, keep)
    return len(stale)


def _fingerprint(title: str) -> str:
    """标题指纹：剥掉日期、序号、'进展/补充'等修饰，识别同一事项。"""
    import re

    key = re.sub(r"[（(].*?[)）]|\d+", "", title)
    for word in ("进展", "补充", "第一次", "第次", "暨"):
        key = key.replace(word, "")
    return re.sub(r"\s+", "", key)[:40]


def cluster(events: list[dict]) -> list[dict]:
    """同事项归并，保留最新一条，附加同期数量备注。"""
    groups: dict[str, list[dict]] = {}
    for e in events:
        groups.setdefault(f"{e.get('entity')}|{_fingerprint(e['title'])}", []).append(e)

    merged = []
    for items in groups.values():
        items.sort(key=lambda x: x.get("published_at") or "", reverse=True)
        head = dict(items[0])
        # 记录整组成员，供推送成功后一并标记已发，避免同组残余
        # 事件在下一轮被当成"新信号"重复推送。
        head["_cluster_ids"] = [i["id"] for i in items]
        if len(items) > 1:
            head["_cluster_size"] = len(items)
            head["_cluster_oldest"] = items[-1].get("published_at") or ""
        merged.append(head)

    merged.sort(
        key=lambda x: (x["relevance"], x.get("published_at") or ""), reverse=True
    )
    return merged


def mark_sent(conn, event_id: int, channel: str, ok: bool, error: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO alerts_sent (event_id, channel, sent_at, ok, error) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_id, channel, db.now_iso(), 1 if ok else 0, error[:400]),
    )


def mark_cluster_sent(conn, event: dict, channel: str, ok: bool, error: str = "") -> None:
    """标记整组事件已推送，防止同组残余在下一轮重复告警。"""
    for eid in event.get("_cluster_ids") or [event["id"]]:
        mark_sent(conn, eid, channel, ok, error)


# ── 渲染 ────────────────────────────────────────────────────

def render_text(events: list[dict]) -> str:
    lines = [f"【竞品情报】发现 {len(events)} 条新信号", ""]
    for e in events:
        lines.append(f"■ {e.get('entity') or '—'} | {e['title']}")
        lines.append(f"  来源: {e['source_name']} | 相关度: {e['relevance']}")
        if e.get("published_at"):
            lines.append(f"  时间: {e['published_at']}")
        if e.get("_cluster_size"):
            lines.append(
                f"  另有 {e['_cluster_size'] - 1} 条同期进展"
                f"（最早 {e.get('_cluster_oldest')}）"
            )
        if e.get("url"):
            lines.append(f"  {e['url']}")
        lines.append("")
    return "\n".join(lines)


def render_markdown(events: list[dict]) -> str:
    lines = [f"## 竞品情报 · 新信号 {len(events)} 条", ""]
    for e in events:
        extra = ""
        if e.get("_cluster_size"):
            extra = (
                f"  \n_另有 {e['_cluster_size'] - 1} 条同期进展"
                f"（最早 {e.get('_cluster_oldest')}）_"
            )
        lines.append(
            f"**{e.get('entity') or '—'}** [{e['title']}]({e.get('url') or '#'})  \n"
            f"`{e['source_name']}` · 相关度 {e['relevance']} · "
            f"{e.get('published_at') or e['captured_at']}  \n"
            f"标签: {e.get('tags')}{extra}"
        )
        lines.append("")
    return "\n".join(lines)


# ── 渠道实现 ────────────────────────────────────────────────

def send_email(cfg: dict, events: list[dict]) -> tuple[bool, str]:
    c = cfg.get("channels", {}).get("email", {}) or {}
    if not c.get("enabled"):
        return False, "disabled"

    user = os.environ.get(c.get("username_env", ""), "")
    pwd = os.environ.get(c.get("password_env", ""), "")
    if not user or not pwd:
        return False, f"缺少环境变量 {c.get('username_env')}/{c.get('password_env')}"

    to_addrs = [a for a in (c.get("to_addrs") or []) if a]
    if not to_addrs:
        return False, "未配置收件人 to_addrs"

    from_addr = c.get("from_addr") or user
    subject = f"{c.get('subject_prefix', '')} 新信号 {len(events)} 条"
    msg = MIMEText(render_text(events), "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)

    try:
        with smtplib.SMTP(c["smtp_host"], int(c.get("smtp_port", 587)), timeout=30) as s:
            if c.get("use_tls", True):
                s.starttls()
            s.login(user, pwd)
            s.sendmail(from_addr, to_addrs, msg.as_string())
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def send_dingtalk(cfg: dict, events: list[dict]) -> tuple[bool, str]:
    c = cfg.get("channels", {}).get("dingtalk", {}) or {}
    if not c.get("enabled"):
        return False, "disabled"

    url = os.environ.get(c.get("webhook_env", ""), "")
    if not url:
        return False, f"缺少环境变量 {c.get('webhook_env')}"

    # 加签安全设置
    secret = os.environ.get(c.get("secret_env", ""), "")
    if secret:
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{secret}"
        digest = hmac.new(
            secret.encode(), string_to_sign.encode(), digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        url = f"{url}&timestamp={ts}&sign={sign}"

    keyword = c.get("keyword", "")
    title = f"{keyword} · 新信号 {len(events)} 条"
    text = render_markdown(events)

    try:
        resp = httpx.post(
            url,
            json={"msgtype": "markdown", "markdown": {"title": title, "text": text}},
            timeout=20,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errcode", 0) != 0:
            return False, f"钉钉返回错误: {body}"
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def detect_platform(url: str) -> str:
    """按 URL 特征识别平台，决定用哪种 JSON 格式。

    各平台消息体格式互不兼容，识别错误对方会直接返回错误码。
    """
    u = url.lower()
    if "qyapi.weixin.qq.com" in u:
        return "wecom"          # 企业微信
    if "open.feishu.cn" in u or "open.larksuite.com" in u:
        return "feishu"         # 飞书
    if "hooks.slack.com" in u:
        return "slack"          # Slack
    if "oapi.dingtalk.com" in u:
        return "dingtalk"       # 钉钉
    return "generic"            # 自建 / n8n / Server酱 等


def _build_payload(platform: str, events: list[dict]) -> dict:
    title = f"竞品情报 · 新信号 {len(events)} 条"
    text = render_text(events)

    if platform == "wecom":
        # 企业微信：纯文本，超长会截断，需控制在 2048 字节内
        return {"msgtype": "text", "text": {"content": f"{title}\n\n{text}"[:2000]}}

    if platform == "feishu":
        # 飞书：text 与 post 两种；用 text 最省事
        return {"msg_type": "text", "content": {"text": f"{title}\n\n{text}"}}

    if platform == "slack":
        return {"text": f"*{title}*\n```{text}```"}

    if platform == "dingtalk":
        return {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": render_markdown(events)},
        }

    # 通用：结构化 JSON，便于自建服务消费
    return {
        "title": title,
        "text": text,
        "count": len(events),
        "events": [
            {
                "entity": e.get("entity"),
                "title": e["title"],
                "url": e.get("url"),
                "source": e["source_name"],
                "relevance": e["relevance"],
                "published_at": e.get("published_at"),
                "tags": e.get("tags"),
            }
            for e in events
        ],
    }


def _check_response(platform: str, resp) -> tuple[bool, str]:
    """各平台的业务错误码藏在 body 里，HTTP 200 不代表成功。"""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return True, ""  # 非 JSON 响应视为成功（如自建服务返回纯文本）

    if platform == "wecom" and body.get("errcode", 0) != 0:
        return False, f"企业微信错误: {body}"
    if platform == "feishu" and body.get("code", 0) != 0:
        return False, f"飞书错误: {body}"
    if platform == "dingtalk" and body.get("errcode", 0) != 0:
        return False, f"钉钉错误: {body}"
    return True, ""


def send_webhook(cfg: dict, events: list[dict]) -> tuple[bool, str]:
    c = cfg.get("channels", {}).get("webhook", {}) or {}
    if not c.get("enabled"):
        return False, "disabled"

    url = os.environ.get(c.get("url_env", ""), "")
    if not url:
        return False, f"缺少环境变量 {c.get('url_env')}"

    platform = detect_platform(url)
    payload = _build_payload(platform, events)

    try:
        resp = httpx.post(url, json=payload, timeout=int(c.get("timeout", 15)))
        resp.raise_for_status()
        ok, err = _check_response(platform, resp)
        if not ok:
            return False, err
        log.info("webhook 平台识别为 [%s]", platform)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


SENDERS = {
    "email": send_email,
    "dingtalk": send_dingtalk,
    "webhook": send_webhook,
}


def run(dry_run: bool = False) -> dict[str, int]:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        log.info("告警已在 alerts.yaml 中关闭")
        return {"sent": 0, "failed": 0, "skipped": 0}

    stats = {"sent": 0, "failed": 0, "skipped": 0}

    with db.connect() as conn:
        # 确保告警表存在（老库可能没有）
        conn.executescript(
            (ROOT / "db" / "schema_alert.sql").read_text(encoding="utf-8")
        )

        for name, sender in SENDERS.items():
            events = collect_new(conn, cfg, name)
            if not events:
                log.info("[%s] 无新信号", name)
                stats["skipped"] += 1
                continue

            # 积压保护：只对启用的渠道生效，避免把历史存量逐轮补推
            if not dry_run and cfg.get("channels", {}).get(name, {}).get("enabled"):
                absorbed = absorb_backlog(conn, cfg, name, len(events))
                if absorbed:
                    stats["absorbed"] = stats.get("absorbed", 0) + absorbed
                    events = collect_new(conn, cfg, name)
                    if not events:
                        log.info("[%s] 全部为历史存量，本次不推送", name)

            if dry_run:
                log.info("[%s] DRY-RUN 将推送 %d 条:\n%s", name, len(events),
                         render_text(events)[:1500])
                stats["sent"] += 1
                continue

            ok, err = sender(cfg, events)
            if ok:
                for e in events:
                    mark_cluster_sent(conn, e, name, True)
                log.info("[%s] 已推送 %d 条", name, len(events))
                stats["sent"] += 1
            elif err == "disabled":
                log.info("[%s] 未启用，跳过", name)
                stats["skipped"] += 1
            else:
                # 失败标记为未成功，下次会重试
                for e in events:
                    mark_cluster_sent(conn, e, name, False, err)
                log.warning("[%s] 推送失败: %s", name, err)
                stats["failed"] += 1

    return stats
