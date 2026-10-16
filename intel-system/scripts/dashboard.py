"""竞品情报看板 —— 零依赖单文件服务（Python 标准库）。

启动：
    python scripts/dashboard.py            # 默认 http://127.0.0.1:8000
    python scripts/dashboard.py --port 9000

设计取舍：
  - 只用标准库（http.server + sqlite3），不引入 Flask/前端构建链。
  - 前端是内嵌的单页 HTML，靠 fetch 调 JSON 接口，无 npm、无打包。
  - 仅监听 127.0.0.1，不对外暴露，因此不做鉴权（内部单人工具）。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common import db  # noqa: E402

# ── 常量 ────────────────────────────────────────────────────

CHANNEL_LABEL = {
    "announcement": "上市公告",
    "tender": "招投标",
    "policy": "政策",
    "site_change": "官网变更",
    "media": "行业媒体",
    "finance": "融资/股本",
    "analysis": "深度分析",
    "analysis_src": "分析来源",
}

# 标签栏内容按 Tab 切换 —— 两个 Tab 用的是完全不同的标签体系。
#
# 踩过的坑：深度分析 Tab 的记录只带「深度分析」「搜索来源」两个标签，
# 而标签栏固定显示下面那 9 个"信号类型"。用户点任何标签都得到 0 条，
# 看起来就是"筛选坏了"。所以标签栏必须随 Tab 变化。
#
# 不含「已解析」：那是采集流程的内部状态（详情页是否抓过），
# 对判读情报没有价值，暴露出来只会让筛选栏变长。
DAILY_TAGS = [
    "重大变更", "融资动态", "融资信号", "订单信号", "新品发布", "竞品命中",
    "官网变更", "中标", "招标",
]
DEEP_TAGS = ["深度分析", "搜索来源"]

# 兼容旧调用
ALL_TAGS = DAILY_TAGS

TAB_TAGS = {"daily": DAILY_TAGS, "deep": DEEP_TAGS}

# 各 Tab 的默认相关度门槛。
# 深度分析的 relevance 是人工设定的固定值（报告 0.9 / 来源 0.5），
# 用"≥0.7"这种面向采集数据的门槛会把 222 条来源全部滤掉 ——
# 表现为"什么筛选都没反应"。所以该 Tab 默认不带门槛。
TAB_MIN_REL = {"daily": "0.7", "deep": ""}

SORTABLE = {
    "published_at": "e.published_at",
    "captured_at": "e.captured_at",
    "relevance": "e.relevance",
    "entity": "e.entity",
    "channel": "e.channel",
}

# 各 Tab 的默认时间范围（与前端 TAB_DAYS 保持一致）。
# 用于让统计卡按"点击后"的口径计数，避免数字在点击前后跳变。
TAB_DAYS_DEFAULT = {"daily": "7", "deep": "all"}


# ── 查询层 ──────────────────────────────────────────────────

def build_query(q: dict) -> tuple[str, list]:
    """把筛选参数翻译成 SQL。所有值走参数绑定，避免注入。"""
    where: list[str] = []
    args: list = []

    if q.get("entity"):
        where.append("e.entity = ?")
        args.append(q["entity"])
    # channels：多值（Tab 切换时传入一组频道），与单值 channel 并存兼容
    chans = [c for c in (q.get("channels") or "").split(",") if c]
    if chans:
        where.append("e.channel IN (" + ",".join("?" * len(chans)) + ")")
        args += chans
    elif q.get("channel"):
        where.append("e.channel = ?")
        args.append(q["channel"])
    if q.get("status"):
        where.append("e.status = ?")
        args.append(q["status"])
    if q.get("starred") == "1":
        where.append("COALESCE(e.starred,0) = 1")
    if q.get("tier"):
        where.append("e.entity_tier = ?")
        args.append(q["tier"])

    # 标签为多选：命中任一即可
    tags = [t for t in (q.get("tags") or "").split(",") if t]
    if tags:
        where.append("(" + " OR ".join(["e.tags LIKE ?"] * len(tags)) + ")")
        args += [f"%{t}%" for t in tags]

    # 时间范围
    days = q.get("days")
    if days and days != "all":
        try:
            since = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
            where.append("COALESCE(NULLIF(e.published_at,''), e.captured_at) >= ?")
            args.append(since)
        except ValueError:
            pass

    if q.get("min_relevance"):
        try:
            where.append("e.relevance >= ?")
            args.append(float(q["min_relevance"]))
        except ValueError:
            pass

    # 关键词：标题 + 摘要 + 实体
    kw = (q.get("q") or "").strip()
    if kw:
        where.append("(e.title LIKE ? OR e.summary LIKE ? OR e.entity LIKE ?)")
        args += [f"%{kw}%"] * 3

    sql = "SELECT e.* FROM events e"
    if where:
        sql += " WHERE " + " AND ".join(where)

    sort = SORTABLE.get(q.get("sort") or "", "e.published_at")
    order = "ASC" if (q.get("order") or "desc").lower() == "asc" else "DESC"
    # 排序键为空的行（部分采集源无发布时间）排到最后
    sql += f" ORDER BY COALESCE(NULLIF({sort},''), e.captured_at) {order}"
    return sql, args


def clean_title(row: dict) -> str:
    """压缩冗长的招标标题。

    政采标题形如「XX代理公司关于YYY项目的公开招标公告」，
    代理公司名对判读无价值，占屏却很长。这里剥掉代理前缀，
    保留"谁在买什么"这个真正有信息量的部分。
    """
    t = (row.get("title") or "").strip()
    if row.get("channel") != "tender":
        return t

    # 去掉「XX公司关于」式前缀
    t = re.sub(r"^.{2,40}?(有限公司|有限责任公司|咨询集团|招标代理|采购中心)关于", "", t)
    # 统一"中标（成交）结果公告"等冗长后缀的括号变体
    t = t.replace("中标（成交）结果公告", "中标公告").replace("中标(成交)结果公告", "中标公告")
    t = t.replace("公开招标公告", "招标公告")
    return t.strip() or (row.get("title") or "").strip()


def rows_to_dicts(rows) -> list[dict]:
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["tags_list"] = json.loads(d.get("tags") or "[]")
        except json.JSONDecodeError:
            d["tags_list"] = []
        d["channel_label"] = CHANNEL_LABEL.get(d.get("channel"), d.get("channel") or "")
        d["title_raw"] = d.get("title") or ""
        d["title"] = clean_title(d)

        # 深度分析报告：关联 reports 表，供前端拉取全文
        d["report_id"] = None
        if d.get("channel") == "analysis" and d.get("entity"):
            try:
                with db.connect() as _c:
                    _r = _c.execute(
                        "SELECT id FROM reports WHERE competitor=? "
                        "ORDER BY id DESC LIMIT 1",
                        (d["entity"],),
                    ).fetchone()
                if _r:
                    d["report_id"] = _r["id"]
            except Exception:  # noqa: BLE001
                pass

        # 右侧「提炼」区。各频道数据形态不同，来源也不同：
        #   media        —— LLM 摊开讲的 2~4 句，退化时用正文前段
        #   analysis     —— 深度报告的战略摘要（从 reports.strategy 取）
        #   analysis_src —— 搜索来源，说明其用途与可信度
        #   tender 等    —— 结构化字段拼装（中标供应商/金额等）
        from src.report import digest as _digest

        llm_d = (d.get("llm_digest") or "").strip()
        d["digest"] = llm_d or _digest.build(d)
        # 影响判断：LLM 结论优先，否则用规则兜底
        d["impact"] = (d.get("llm_impact") or "").strip() or _digest.impact(d)

        brief, brief_label = "", ""
        if d.get("channel") == "analysis":
            # 深度报告：优先用 LLM 提炼的要点（3~5 句，200 字左右）。
            # 退化顺序：brief -> strategy 段落 -> summary。
            # strategy 是报告原文节选（3000+ 字），仅作兜底。
            row = None
            if d.get("report_id"):
                try:
                    with db.connect() as _c:
                        row = _c.execute(
                            "SELECT brief, strategy, score FROM reports WHERE id=?",
                            (d["report_id"],),
                        ).fetchone()
                except Exception:  # noqa: BLE001
                    row = None
            if row and (row["brief"] or "").strip():
                brief = row["brief"].strip()
                brief_label = "提炼 · 点「查看完整报告」读全文"
            elif row and (row["strategy"] or "").strip():
                brief = row["strategy"].strip()
                brief_label = "战略摘要（未提炼原文）"
            elif d.get("summary"):
                brief = (d.get("summary") or "").strip()

        elif d.get("channel") == "analysis_src":
            # 搜索来源：本栏不重复标题，而是说明它的用途和可信度
            brief = (
                f"这是深度分析「{d.get('entity') or '该竞品'}」时由 Tavily 检索到的来源链接。\n\n"
                f"用途：为分析结论提供可追溯依据；点「打开原文」可查看原文。\n\n"
                f"注意：此类条目仅记录链接，未经人工核验，可信度低于直采数据"
                f"（政采、公告、官网）。若你需要该竞品的结论性内容，"
                f"请查看其「深度分析报告」条目。"
            )
            brief_label = "来源说明"

        elif d.get("channel") == "site_change":
            # 官网变更：说明「变了什么」以及为何需要人工核对
            # （只存了内容指纹，不做 diff，无法自动判断变更性质）
            brief = (
                f"监控的竞品官网页面内容发生变化。\n\n"
                f"来源页面：{d.get('url') or '（未记录）'}\n\n"
                f"说明：系统只比对页面内容指纹，不记录差异明细，"
                f"因此无法判断改动性质（可能是发新品、调价、更新案例等）。"
                f"请点「打开原文」人工核对本次变更是否值得关注。"
            )
            brief_label = "变更提示"

        if not brief:
            brief = (d.get("llm_summary") or "").strip()
            if brief:
                brief_label = "提炼"
            else:
                body = (d.get("body") or "").strip()
                if body:
                    brief = body[:400]
                    brief_label = "提炼 · 正文摘录"

        d["summary_long"] = brief
        d["brief_label"] = brief_label
        d["from_llm"] = bool(llm_d)
        d["has_body"] = bool((d.get("body") or "").strip())

        # 从 summary 里抽出中标供应商，单独成字段，便于列表直接显示
        sup = ""
        s = d.get("summary") or ""
        if "中标供应商" in s:
            part = next((p for p in s.split("|") if "中标供应商" in p), "")
            sup = part.replace("中标供应商:", "").strip()
        d["supplier"] = sup
        out.append(d)
    return out


def api_events(q: dict) -> dict:
    sql, args = build_query(q)
    limit = min(int(q.get("limit") or 100), 500)
    offset = max(int(q.get("offset") or 0), 0)
    with db.connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM (" + sql + ")", args
        ).fetchone()[0]
        rows = conn.execute(sql + " LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return {"total": total, "items": rows_to_dicts(rows)}


def _filters_only(q: dict) -> tuple[str, list]:
    """把筛选条件编译成 WHERE 片段（不含 SELECT/ORDER/LIMIT）。

    供 facets 统计使用：构面计数必须与列表查询用同一套过滤规则，
    否则数字与实际结果会对不上。

    build_query 生成的条件带 `e.` 表前缀（列表查询用了别名），
    而统计语句用的是裸表名，这里去掉前缀以复用同一套逻辑。
    """
    full_sql, args = build_query(q)
    head = full_sql.partition(" ORDER BY ")[0]
    where = head.split(" WHERE ", 1)[1] if " WHERE " in head else ""
    return where.replace("e.", ""), args


def api_report(report_id: int) -> dict:
    """取一份完整深度分析报告。"""
    with db.connect() as conn:
        r = conn.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
    if not r:
        return {"error": "报告不存在"}
    d = dict(r)
    # 同时给出关联的 event id，便于前端定位列表项
    with db.connect() as conn:
        ev = conn.execute(
            "SELECT id FROM events WHERE channel='analysis' AND entity=? "
            "ORDER BY id DESC LIMIT 1",
            (d["competitor"],),
        ).fetchone()
    d["event_id"] = ev["id"] if ev else None
    return d


def api_facets(q: dict | None = None) -> dict:
    """筛选器可选项 + 顶部统计卡片。

    q: 当前生效的筛选条件（不含 tags）。标签计数必须在这些条件下统计 ——
    否则会出现"标签显示 10 条、点进去 0 条"的不一致（例如默认「近7天」
    把历史事件排除在外，而计数却是全量的）。
    """
    q = q or {}
    # 复用 build_query 的过滤逻辑，但排除 tags 本身：
    # 每个标签的计数应基于"其余条件"，标签之间仍是 OR 关系。
    # omitTags / omitSignals 是前端传的控制标记，不能进入 SQL 条件
    _NON_FILTER = ("tags", "limit", "offset", "sort", "order",
                   "omitTags", "omitSignals")
    base_q = {k: v for k, v in q.items() if k not in _NON_FILTER}
    where_sql, base_args = _filters_only(base_q)
    cond = f" AND ({where_sql})" if where_sql else ""

    with db.connect() as conn:
        # 竞品下拉：计数必须带当前筛选（cond），否则数字与"点下去看到几条"
        # 对不上 —— 标签计数踩过同样的坑。
        # 另返回 entity_total（有归属竞品的记录总数），供前端显示 "N/总数"。
        ent_rows = [
            {"value": r["entity"], "count": r["n"]}
            for r in conn.execute(
                "SELECT entity, COUNT(*) n FROM events "
                "WHERE entity IS NOT NULL AND entity<>''" + cond +
                " GROUP BY entity ORDER BY n DESC",
                base_args,
            )
        ]
        entities = ent_rows
        entity_total = conn.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE entity IS NOT NULL AND entity<>''" + cond,
            base_args,
        ).fetchone()[0]
        # 频道计数：全局量（不受任何筛选影响）。
        #
        # 口径必须与「全部」统计卡一致 —— 两者都表达"这个范围总共有多少条"。
        # 实测踩过两次坑：Tab 数字曾用"当前筛选下的计数"，导致
        # 「Tab 显示 91、全部卡显示 452」的矛盾。
        #
        # 不限定具体 Tab：前端按 Tab 分组求和，两个 Tab 的数字都要能算出来。
        channels = [
            {"value": r["channel"],
             "label": CHANNEL_LABEL.get(r["channel"], r["channel"]),
             "count": r["n"]}
            for r in conn.execute(
                "SELECT channel, COUNT(*) n FROM events GROUP BY channel "
                "ORDER BY n DESC"
            )
        ]
        today = datetime.now().strftime("%Y-%m-%d")

        # ── 统计卡计数 ────────────────────────────────────────
        # 每张卡显示"点击之后会看到的条数"，而不是"当前条件下的条数"。
        #
        # 原因（实测）：quick() 是重置语义 —— 点卡片会清空 entity/tags/q/
        # min_relevance，并把 days 归位到 Tab 默认值。若计数仍按"当前条件"
        # 算，就会出现「点前 91、点后 261」的数字跳变，看起来像 bug。
        #
        # 做法：为每张卡单独构造"点击后"的条件，再用它计数。
        #   base = 仅保留 Tab 的频道范围（其余筛选都会被清空）
        tab_q = {k: v for k, v in base_q.items() if k == "channels"}

        def _count_with(extra: dict, override_days: str | None = None) -> int:
            qq = dict(tab_q)
            qq.update(extra)
            if override_days is None:
                qq.pop("days", None)          # 全部时间
            else:
                qq["days"] = override_days
            qq.pop("min_relevance", None)     # 卡片一律不带相关度门槛
            w, a = _filters_only(qq)
            sql = "SELECT COUNT(*) FROM events WHERE 1=1" + (f" AND ({w})" if w else "")
            return conn.execute(sql, a).fetchone()[0]

        days_default = TAB_DAYS_DEFAULT.get(
            (q.get("tab") or "daily"), "7"
        )
        stats = {
            # 「全部」卡：清空筛选后的全量（Tab 频道范围内）
            "total": _count_with({}),
            # 「今日新增」：同样是全量口径下的今日
            "today": conn.execute(
                "SELECT COUNT(*) FROM events WHERE captured_at LIKE ?"
                + (f" AND ({_filters_only(tab_q)[0]})" if tab_q else ""),
                [today + "%"] + _filters_only(tab_q)[1],
            ).fetchone()[0],
            "unread": _count_with({"status": "new"}),
            "starred": _count_with({"starred": "1"}),
            # 兼容：旧前端若仍读这些字段
            "days_default": days_default,
            "entity_total": entity_total,
        }
        # 标签计数：按当前 Tab 的标签集算，且限定在"其余筛选条件"下，
        # 保证数字能预测点击后的结果。
        tabs_cfg = TAB_TAGS.get(q.get("tab") or "daily", DAILY_TAGS)
        tag_counts = [
            {
                "name": name,
                "count": conn.execute(
                    "SELECT COUNT(*) FROM events WHERE tags LIKE ?" + cond,
                    [f"%{name}%"] + base_args,
                ).fetchone()[0],
            }
            for name in tabs_cfg
        ]
        # 近 14 天各竞品动态趋势（补齐空缺日期，否则柱子挤在一起看不出节奏）
        raw = {
            r["d"]: r["n"]
            for r in conn.execute(
                "SELECT substr(COALESCE(NULLIF(published_at,''),captured_at),1,10) d,"
                " COUNT(*) n FROM events "
                "WHERE COALESCE(NULLIF(published_at,''),captured_at) >= ? "
                "GROUP BY d ORDER BY d",
                ((datetime.now() - timedelta(days=13)).strftime("%Y-%m-%d"),),
            )
        }
    today_d = datetime.now().date()
    trend = []
    for i in range(13, -1, -1):
        day = (today_d - timedelta(days=i)).isoformat()
        trend.append({"date": day, "count": int(raw.get(day, 0))})
    return {
        "entities": entities,
        "channels": channels,
        "tags": [t["name"] for t in tag_counts],
        "tag_counts": tag_counts,
        "stats": stats,
        "trend": trend,
    }


# 允许前端直接修改的字段白名单，避免任意列写入
EDITABLE = {"status", "note", "starred"}


def api_update(payload: dict) -> dict:
    eid = payload.get("id")
    if not eid:
        return {"ok": False, "error": "缺少 id"}

    sets, args = [], []
    for k, v in (payload.get("fields") or {}).items():
        if k not in EDITABLE:
            continue
        sets.append(f"{k} = ?")
        args.append(v)
    if not sets:
        return {"ok": False, "error": "无可更新字段"}

    sets.append("updated_at = ?")
    args.append(db.now_iso())
    args.append(eid)

    with db.connect() as conn:
        conn.execute(f"UPDATE events SET {', '.join(sets)} WHERE id = ?", args)
    return {"ok": True}


def api_bulk(payload: dict) -> dict:
    """批量标记已读/归档/星标。"""
    ids = payload.get("ids") or []
    action = payload.get("action")
    if not ids:
        return {"ok": False, "error": "未选择记录"}

    field, value = {
        "read": ("status", "read"),
        "unread": ("status", "new"),
        "archive": ("status", "archived"),
        "star": ("starred", 1),
        "unstar": ("starred", 0),
    }.get(action, (None, None))
    if not field:
        return {"ok": False, "error": f"未知操作: {action}"}

    marks = ",".join("?" * len(ids))
    with db.connect() as conn:
        conn.execute(
            f"UPDATE events SET {field} = ?, updated_at = ? WHERE id IN ({marks})",
            [value, db.now_iso()] + list(ids),
        )
    return {"ok": True, "affected": len(ids)}


def api_export(q: dict) -> bytes:
    """导出当前筛选结果为 CSV（Excel 可直接打开）。"""
    sql, args = build_query(q)
    with db.connect() as conn:
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "时间", "竞品", "梯队", "频道", "标题", "相关度",
                "标签", "状态", "链接", "备注"])
    for r in rows:
        w.writerow([
            r["id"],
            r.get("published_at") or r.get("captured_at") or "",
            r.get("entity") or "",
            r.get("entity_tier") or "",
            r.get("channel_label") or "",
            r.get("title") or "",
            r.get("relevance") or "",
            " ".join(r.get("tags_list") or []),
            r.get("status") or "",
            r.get("url") or "",
            r.get("note") or "",
        ])
    # BOM 让 Excel 正确识别 UTF-8
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# ── HTTP 层 ─────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "IntelDashboard/1.0"

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏

    # --- helpers ---
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _query(self) -> dict:
        qs = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in qs.items()}

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # --- routes ---
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/events":
                self._json(api_events(self._query()))
            elif path == "/api/facets":
                self._json(api_facets(self._query()))
            elif path.startswith("/api/report/"):
                try:
                    rid = int(path.rsplit("/", 1)[1])
                except ValueError:
                    self._json({"error": "bad id"}, 400)
                    return
                self._json(api_report(rid))
            elif path == "/api/export":
                data = api_export(self._query())
                stamp = datetime.now().strftime("%Y%m%d_%H%M")
                self._send(200, data, "text/csv; charset=utf-8", {
                    "Content-Disposition":
                        f'attachment; filename="intel_{stamp}.csv"'
                })
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._body()
            if path == "/api/update":
                self._json(api_update(payload))
            elif path == "/api/bulk":
                self._json(api_bulk(payload))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>仓储 AMR 竞品情报看板</title>
<style>

  :root{
    --bg:#0f1420; --panel:#161c2b; --panel2:#1d2537; --line:#28324a;
    --fg:#e6ebf5; --dim:#8b97b0; --accent:#4f9cf9; --ok:#37c98b;
    --warn:#f5a623; --danger:#f2555a; --major:#ff5c8a; --order:#37c98b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.55 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  header{padding:16px 20px;border-bottom:1px solid var(--line);
         display:flex;flex-wrap:wrap;gap:14px;align-items:center;
         background:var(--panel);position:sticky;top:0;z-index:20}
  h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.3px}
  h1 span{color:var(--dim);font-weight:400;font-size:12px;margin-left:8px}
  .spacer{flex:1}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));
         gap:10px;padding:14px 20px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
        padding:10px 12px;cursor:pointer;transition:.15s;position:relative}
  .card:hover{border-color:var(--accent);transform:translateY(-1px)}
  .card.active{border-color:var(--accent);background:var(--panel2)}
  .card .k{color:var(--dim);font-size:11px;letter-spacing:.4px}
  .card .v{font-size:21px;font-weight:600;margin-top:3px}
  .filters{padding:0 20px 14px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
  select,input,button{background:var(--panel2);color:var(--fg);
    border:1px solid var(--line);border-radius:6px;padding:7px 10px;font:inherit;font-size:13px}
  input[type=search]{min-width:220px}
  button{cursor:pointer}
  button:hover{border-color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  .tags{display:flex;gap:6px;flex-wrap:wrap;flex-basis:100%}
  .hint{color:var(--dim);font-size:12px;flex-basis:100%;margin-bottom:2px}
  .hint b{color:var(--accent);font-weight:600}
  .tag{padding:5px 10px;border-radius:20px;border:1px solid var(--line);
       background:var(--panel2);cursor:pointer;font-size:12px;user-select:none;
       display:inline-flex;align-items:center;gap:6px;transition:.12s}
  .tag:hover{border-color:var(--accent)}
  .tag b{font-weight:600;font-size:11px;color:var(--dim);
         background:rgba(255,255,255,.06);padding:1px 6px;border-radius:9px}
  .tag.on{background:var(--accent);border-color:var(--accent);color:#fff}
  .tag.on b{color:#fff;background:rgba(0,0,0,.22)}
  .tag.zero{opacity:.4}
  .trend{padding:0 20px 12px;display:flex;gap:3px;align-items:flex-end;height:46px}
  .trend .bar{flex:1;background:var(--accent);opacity:.55;border-radius:2px 2px 0 0;
              min-height:2px;position:relative}
  .trend .bar:hover{opacity:1}
  .trend .bar span{position:absolute;bottom:100%;left:50%;transform:translateX(-50%);
    background:var(--panel2);border:1px solid var(--line);padding:2px 6px;border-radius:4px;
    font-size:11px;white-space:nowrap;display:none}
  .trend .bar:hover span{display:block}
  .toolbar{padding:0 20px 10px;display:flex;gap:10px;align-items:center;color:var(--dim);font-size:12px}
  main{padding:0 20px 60px;display:flex;flex-direction:column;gap:8px}
  .row{background:var(--panel);border:1px solid var(--line);border-radius:8px;
       padding:11px 13px;display:flex;gap:12px;transition:.12s;align-items:flex-start}
  /* 有提炼内容时切成两栏：左侧信息 + 右侧摊开的几句话 */
  .row.two-col .body{flex:1 1 58%;min-width:0}
  .brief{flex:1 1 42%;min-width:0;border-left:1px solid var(--line);
         padding-left:14px;margin-left:2px}
  .brief-h{font-size:11px;color:var(--accent);letter-spacing:.6px;margin-bottom:6px;
           text-transform:uppercase}
  .brief-b{font-size:13px;line-height:1.7;color:#c8d2e4;white-space:pre-wrap;
           word-break:break-word;max-height:260px;overflow:auto}
  @media (max-width:1100px){
    .row.two-col{flex-direction:column}
    .brief{flex:1 1 100%;border-left:none;border-top:1px solid var(--line);
           padding-left:0;padding-top:10px;margin-left:0}
  }
  .row:hover{border-color:#38507a}
  .row.read{opacity:.5}
  .row.starred{border-left:3px solid var(--warn)}
  .row.major{border-left:3px solid var(--major)}
  .row.order{border-left:3px solid var(--order)}
  .row input[type=checkbox]{margin-top:3px;width:15px;height:15px;cursor:pointer;flex:none}
  .body{flex:1;min-width:0}
  /* 提炼摘要：列表里第一眼看到的就是它 */
  .digest{font-size:14.5px;font-weight:500;line-height:1.5;color:var(--fg);
          word-break:break-word}
  /* 影响判断：次要一层，颜色上区分开 */
  .impact{font-size:12.5px;color:#8fd6b4;margin-top:4px;
          padding-left:9px;border-left:2px solid rgba(55,201,139,.4)}
  /* 原始标题：降级为小字副标题，保留可追溯性但不抢视线 */
  .title-sub{font-size:12px;color:var(--dim);margin-top:5px;
             word-break:break-word;overflow:hidden;text-overflow:ellipsis;
             display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
  .title{font-weight:500;word-break:break-word}
  .meta{color:var(--dim);font-size:12px;margin-top:5px;
        display:flex;flex-wrap:wrap;gap:10px;align-items:center}
  .pill{padding:2px 7px;border-radius:4px;background:var(--panel2);
        border:1px solid var(--line);font-size:11px}
  .pill.major{background:rgba(255,92,138,.16);border-color:var(--major);color:var(--major)}
  .pill.order{background:rgba(55,201,139,.16);border-color:var(--order);color:var(--order)}
  .pill.hit{background:rgba(79,156,249,.16);border-color:var(--accent);color:var(--accent)}
  .pill.sup{background:rgba(255,196,0,.14);border-color:#ffc400;color:#ffc400}
  /* 深度报告弹层 */
  .modal-mask{position:fixed;inset:0;background:rgba(0,0,0,.66);z-index:100;
              display:none;align-items:center;justify-content:center;padding:24px}
  .modal-mask.show{display:flex}
  .modal{background:var(--panel);border:1px solid var(--line);border-radius:10px;
         width:min(1000px,94vw);max-height:92vh;display:flex;flex-direction:column}
  .modal-h{padding:14px 18px;border-bottom:1px solid var(--line);
           display:flex;align-items:center;gap:12px;font-weight:600}
  .modal-b{padding:20px 24px;overflow:auto;line-height:1.75;font-size:13.5px}
  .modal-b h1,.modal-b h2,.modal-b h3{color:#fff;margin:1.2em 0 .5em;line-height:1.4}
  .modal-b h1{font-size:19px}.modal-b h2{font-size:16px}.modal-b h3{font-size:14px}
  .modal-b table{border-collapse:collapse;width:100%;margin:.8em 0;font-size:12.5px}
  .modal-b th,.modal-b td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
  .modal-b th{background:var(--panel2)}
  .modal-b code{background:var(--panel2);padding:1px 5px;border-radius:3px;font-size:12px}
  .modal-b blockquote{border-left:3px solid var(--accent);margin:.6em 0;
                      padding:.4em 12px;color:var(--dim);background:rgba(79,156,249,.06)}
  .modal-b ul,.modal-b ol{padding-left:1.4em}
  .modal-b a{color:var(--accent)}
  .modal-b pre{background:var(--panel2);padding:10px 12px;border-radius:6px;overflow:auto}
  .btn-close{margin-left:auto}
  /* 深度报告弹层 */
  .modal-mask{position:fixed;inset:0;background:rgba(0,0,0,.66);z-index:100;
              display:none;align-items:center;justify-content:center;padding:24px}
  .modal-mask.show{display:flex}
  .modal{background:var(--panel);border:1px solid var(--line);border-radius:10px;
         width:min(1000px,94vw);max-height:92vh;display:flex;flex-direction:column}
  .modal-h{padding:14px 18px;border-bottom:1px solid var(--line);
           display:flex;align-items:center;gap:12px;font-weight:600}
  .modal-b{padding:20px 24px;overflow:auto;line-height:1.75;font-size:13.5px}
  .modal-b h1,.modal-b h2,.modal-b h3{color:#fff;margin:1.2em 0 .5em;line-height:1.4}
  .modal-b h1{font-size:19px}.modal-b h2{font-size:16px}.modal-b h3{font-size:14px}
  .modal-b table{border-collapse:collapse;width:100%;margin:.8em 0;font-size:12.5px}
  .modal-b th,.modal-b td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
  .modal-b th{background:var(--panel2)}
  .modal-b code{background:var(--panel2);padding:1px 5px;border-radius:3px;font-size:12px}
  .modal-b blockquote{border-left:3px solid var(--accent);margin:.6em 0;
                      padding:.4em 12px;color:var(--dim);background:rgba(79,156,249,.06)}
  .modal-b ul,.modal-b ol{padding-left:1.4em}
  .modal-b a{color:var(--accent)}
  .btn-close{margin-left:auto}
  .note{margin-top:7px;width:100%;background:var(--panel2);color:var(--fg);
        border:1px solid var(--line);border-radius:5px;padding:6px 8px;font-size:12px;
        display:none}
  .note.show{display:block}
  .acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
  .acts button{padding:4px 9px;font-size:12px}
  .empty{text-align:center;color:var(--dim);padding:70px 0}
  .selbar{position:fixed;bottom:0;left:0;right:0;background:var(--panel);
    border-top:1px solid var(--line);padding:10px 20px;display:none;
    gap:10px;align-items:center;z-index:30}
  .selbar.show{display:flex}
  .loading{opacity:.5;pointer-events:none}
  footer{padding:10px 20px;color:var(--dim);font-size:12px;
         border-top:1px solid var(--line);text-align:center}
  /* Tab 栏：区分「每日情报流」与「深度分析」两类数据 */
  .tabs{display:flex;gap:4px;padding:12px 20px 0;border-bottom:1px solid var(--line);
        background:var(--panel);position:sticky;top:57px;z-index:19}
  .tab{background:transparent;border:1px solid transparent;border-bottom:none;
       border-radius:8px 8px 0 0;padding:9px 18px;color:var(--dim);
       font-size:14px;font-weight:500;cursor:pointer;transition:.15s;
       display:inline-flex;align-items:center;gap:8px}
  .tab:hover{color:var(--fg);background:var(--panel2)}
  .tab.active{background:var(--bg);color:var(--fg);
              border-color:var(--line);margin-bottom:-1px;
              border-bottom:1px solid var(--bg)}
  .tab-n{font-size:11px;color:var(--dim);background:var(--panel2);
         padding:1px 7px;border-radius:9px}
  .tab.active .tab-n{color:var(--accent);background:rgba(79,156,249,.14)}
  /* ── 新增控件样式 ─────────────────────────────── */
  /* ── 多选下拉（信号类型）────────────────────────────── */
  .msel{position:relative}
  .msel-btn{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
            border-radius:6px;padding:7px 10px;font:inherit;font-size:13px;
            cursor:pointer;display:inline-flex;align-items:center;gap:6px}
  .msel-btn:hover{border-color:var(--accent)}
  .msel-caret{color:var(--dim);font-size:10px}
  .msel-badge{background:var(--accent);color:#fff;font-size:11px;
              padding:0 6px;border-radius:9px;display:none}
  .msel-badge.show{display:inline-block}
  .msel-menu{position:absolute;top:calc(100% + 4px);left:0;min-width:190px;
             background:var(--panel2);border:1px solid var(--line);border-radius:8px;
             padding:6px;z-index:40;display:none;box-shadow:0 8px 24px rgba(0,0,0,.4)}
  .msel-menu.show{display:block}
  .msel-item{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:5px;
             cursor:pointer;font-size:13px;white-space:nowrap}
  .msel-item:hover{background:rgba(79,156,249,.12)}
  .msel-item input{cursor:pointer}
  .msel-item .n{color:var(--dim);font-size:11px;margin-left:auto}

  /* ── 开关（显示低相关度）────────────────────────────── */
  .switch{display:inline-flex;align-items:center;gap:6px;color:var(--dim);
          font-size:13px;cursor:pointer;user-select:none;padding:7px 2px}
  .switch:hover{color:var(--fg)}
  .switch input{cursor:pointer;width:14px;height:14px;accent-color:var(--accent)}

  /* ── 统计卡：纯展示，无 hover 反馈、无指针 ──────────── */
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
        padding:10px 12px;cursor:default}
</style>
</head>
<body>
<header>
  <h1>仓储 AMR 竞品情报看板</h1>
  <div class="spacer"></div>
  <button onclick="reload()">刷新</button>
  <button onclick="exportCsv()">导出 CSV</button>
  <button onclick="markAllRead()">全部标为已读</button>
</header>

<!-- Tab：只负责切换数据范围。不显示数字，避免与列表条数混淆。 -->
<nav class="tabs">
  <button class="tab active" data-tab="daily" onclick="switchTab('daily')">每日情报</button>
  <button class="tab" data-tab="deep" onclick="switchTab('deep')">深度分析</button>
</nav>

<!-- 统计：纯展示，不可点击。数字是"当前范围内的总数"。 -->
<div class="stats" id="stats"></div>

<div class="trend" id="trend"></div>

<!-- 筛选栏：每个维度只出现一次 -->
<div class="filters">
  <select id="f-entity" onchange="apply()">
    <option value="">全部竞品</option>
  </select>

  <!-- 信号类型：多选下拉。内容随 Tab 动态更换。 -->
  <div class="msel" id="ms-signal">
    <button class="msel-btn" onclick="toggleSignalMenu(event)">
      信号类型<span class="msel-badge" id="ms-signal-badge"></span>
      <span class="msel-caret">▾</span>
    </button>
    <div class="msel-menu" id="ms-signal-menu"></div>
  </div>

  <select id="f-days" onchange="apply()">
    <option value="1">近 1 天</option>
    <option value="7" selected>近 7 天</option>
    <option value="30">近 30 天</option>
    <option value="90">近 90 天</option>
    <option value="365">近 1 年</option>
    <option value="all">全部时间</option>
  </select>

  <select id="f-status" onchange="apply()">
    <option value="">全部状态</option>
    <option value="new">未读</option>
    <option value="read">已读</option>
    <option value="archived">已归档</option>
  </select>

  <!-- 排序合并为一个下拉，不再拆成"字段 + 升降序" -->
  <select id="f-sort" onchange="apply()">
    <option value="published_at:desc">最新优先</option>
    <option value="published_at:asc">最早优先</option>
    <option value="relevance:desc">相关度优先</option>
    <option value="entity:asc">按竞品名</option>
  </select>

  <input type="search" id="f-q" placeholder="搜索标题 / 摘要 / 竞品…" oninput="debounced()">

  <!-- 低相关度开关：替代原"相关度"下拉，语义更直白 -->
  <label class="switch" title="勾选后显示系统判定价值较低的内容">
    <input type="checkbox" id="f-lowrel" onchange="apply()">
    <span>显示低相关度</span>
  </label>
</div>

<div class="toolbar">
  <span id="count"></span>
  <span id="selinfo"></span>
</div>

<main id="list"></main>

<div class="selbar" id="selbar">
  <span id="selcount"></span>
  <div class="spacer"></div>
  <button onclick="bulk('read')">标为已读</button>
  <button onclick="bulk('unread')">标为未读</button>
  <button onclick="bulk('star')">加星</button>
  <button onclick="bulk('unstar')">取消星标</button>
  <button onclick="bulk('archive')">归档</button>
  <button onclick="clearSel()">取消选择</button>
</div>

<footer>数据源：上市公告 / 招投标 / 官网变更 / 政策 / 行业媒体 / 深度分析 · 本地运行，仅监听 127.0.0.1</footer>

<!-- 深度报告弹层 -->
<div class="modal-mask" id="reportMask" onclick="if(event.target===this)closeReport()">
  <div class="modal">
    <div class="modal-h">
      <span id="reportTitle">报告</span>
      <button class="btn-close" onclick="closeReport()">关闭</button>
    </div>
    <div class="modal-b" id="reportBody">加载中…</div>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════════
//  状态
// ═══════════════════════════════════════════════════════════
const S = {
  tab: 'daily',          // daily | deep
  signals: new Set(),    // 选中的信号类型（原"标签"）
  items: [],
  sel: new Set(),
};

// 各 Tab 的范围配置。每个维度只在一处定义，避免多处不一致。
const TABS = {
  daily: {
    channels: ['media','tender','announcement','policy','finance','site_change'],
    days: '7',
    signals: ['重大变更','融资动态','融资信号','订单信号','新品发布',
              '竞品命中','官网变更','中标','招标'],
    lowRelDefault: false,   // 默认只看高相关
  },
  deep: {
    channels: ['analysis','analysis_src'],
    days: 'all',
    signals: ['深度分析','搜索来源'],
    lowRelDefault: true,    // 深度分析的数据相关度是人工值，默认全看
  },
};

const $ = id => document.getElementById(id);
const esc = s => (s==null?'':String(s)).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, opts){
  const r = await fetch(path, opts);
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}

// ═══════════════════════════════════════════════════════════
//  请求参数 —— 所有筛选条件在此集中组装
// ═══════════════════════════════════════════════════════════
function params(extra){
  const cfg = TABS[S.tab];
  const p = new URLSearchParams();

  p.set('channels', cfg.channels.join(','));
  p.set('tab', S.tab);

  if($('f-entity').value) p.set('entity', $('f-entity').value);
  if(S.signals.size)      p.set('tags', [...S.signals].join(','));
  if($('f-days').value)   p.set('days', $('f-days').value);
  if($('f-status').value) p.set('status', $('f-status').value);
  if($('f-q').value)      p.set('q', $('f-q').value);

  // 排序："字段:方向" 拆开传，兼容后端既有参数名
  const [sf, so] = ($('f-sort').value || 'published_at:desc').split(':');
  p.set('sort', sf);
  p.set('order', so || 'desc');

  // 「显示低相关度」开关：
  //   勾上   -> 不施加门槛，全部显示
  //   不勾上 -> 只显示相关度 >= 门槛的内容
  // 门槛固定 0.7：这是采集数据的天然分界（高价值信号都在 0.7 以上），
  // 不做成可选项是为了减少一个需要理解的旋钮。
  if(!$('f-lowrel').checked) p.set('min_relevance', '0.7');

  for(const k in (extra||{})) p.set(k, extra[k]);
  return p.toString();
}

// ═══════════════════════════════════════════════════════════
//  Tab 切换
// ═══════════════════════════════════════════════════════════
async function switchTab(name){
  if(!TABS[name]) return;
  S.tab = name;
  document.querySelectorAll('.tab').forEach(el=>
    el.classList.toggle('active', el.dataset.tab === name));

  // 切换 Tab = 换一套数据，所有筛选条件重置为该 Tab 的默认值
  S.signals.clear();
  S.sel.clear(); renderSel();
  $('f-entity').value = '';
  $('f-q').value = '';
  $('f-status').value = '';
  $('f-sort').value = 'published_at:desc';
  $('f-days').value = TABS[name].days;
  $('f-lowrel').checked = !!TABS[name].lowRelDefault;

  await apply();
}

// ═══════════════════════════════════════════════════════════
//  信号类型多选下拉
// ═══════════════════════════════════════════════════════════
function toggleSignalMenu(ev){
  ev.stopPropagation();
  const m = $('ms-signal-menu');
  m.classList.toggle('show');
}

document.addEventListener('click', () => {
  $('ms-signal-menu').classList.remove('show');
});

function toggleSignal(name){
  S.signals.has(name) ? S.signals.delete(name) : S.signals.add(name);
  renderSignalMenu();
  apply();
}

let _signalCounts = {};

function renderSignalMenu(){
  const cfg = TABS[S.tab];
  $('ms-signal-menu').innerHTML = cfg.signals.map(n=>{
    const on = S.signals.has(n);
    const c = _signalCounts[n];
    return `<label class="msel-item" onclick="event.stopPropagation()">
      <input type="checkbox" ${on?'checked':''} onchange="toggleSignal('${esc(n)}')">
      <span>${esc(n)}</span>
      ${c!==undefined?`<span class="n">${c}</span>`:''}
    </label>`;
  }).join('');

  const badge = $('ms-signal-badge');
  badge.textContent = S.signals.size;
  badge.classList.toggle('show', S.signals.size > 0);
}

// ═══════════════════════════════════════════════════════════
//  构面（下拉选项 + 统计）
// ═══════════════════════════════════════════════════════════
async function loadFacets(){
  const f = await api('/api/facets?' + params({omitTags: true, omitSignals: '1'}));

  // ── 竞品下拉：增量更新，绝不重建 ──────────────────────
  //
  // 踩过的坑：原来是整体重建 innerHTML，当用户选的竞品在新条件下
  // 没有记录时，它会从选项里消失，代码随即回退到"全部竞品" ——
  // 于是筛选被静默取消，用户看到的是全部数据却以为筛选生效了。
  // 这是危险的：可能漏掉重要信息还以为看全了。
  //
  // 现在的做法：只更新每个选项的计数文字；用户选中的竞品即使
  // 条数为 0 也保留在列表里（标注 0），让"筛选意图"保持不变。
  const es = $('f-entity');
  const cur = es.value;

  // 1) 保留已有选项（只显示竞品名，不带计数）
  const seen = new Set(['']);
  [...es.options].forEach(o=>{
    if(o.value === '') return;
    seen.add(o.value);
    o.textContent = o.value;
  });

  // 2) 追加新出现的竞品（按计数降序插入到末尾之前）
  f.entities.filter(e => !seen.has(e.value)).forEach(e=>{
    const o = document.createElement('option');
    o.value = e.value;
    o.textContent = e.value;
    es.appendChild(o);
  });

  // 3) 恢复选择 —— 即使该项条数为 0 也保留
  es.value = cur;
  if(es.value !== cur && cur){
    // 极端情况：该项从未出现过（例如用户手改了 URL），补一个占位项
    const o = document.createElement('option');
    o.value = cur; o.textContent = cur;
    es.appendChild(o); es.value = cur;
  }

  // 信号计数（用于多选下拉里显示各有多少条）
  _signalCounts = {};
  (f.tag_counts||[]).forEach(t => { _signalCounts[t.name] = t.count; });
  renderSignalMenu();

  // 统计卡：纯展示，不可点击
  const s = f.stats;
  const cards = [
    ['全部', s.total],
    ['今日新增', s.today],
    ['未读', s.unread],
    ['已加星', s.starred],
  ];
  $('stats').innerHTML = cards.map(([k,v])=>
    `<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`
  ).join('');

  renderTrend(f.trend);
}

function renderTrend(t){
  if(!t || !t.length){ $('trend').innerHTML=''; return; }
  const max = Math.max(...t.map(x=>x.count), 1);
  $('trend').innerHTML = t.map(x=>
    `<div class="bar" style="height:${Math.max(3, x.count/max*100)}%">
       <span>${x.date} · ${x.count} 条</span></div>`).join('');
}

// ═══════════════════════════════════════════════════════════
//  列表
// ═══════════════════════════════════════════════════════════
let _timer = null;
function debounced(){ clearTimeout(_timer); _timer = setTimeout(apply, 260); }

async function apply(){
  $('list').classList.add('loading');
  try{
    await loadFacets();                       // 先刷新构面，保证数字与列表同步
    const data = await api('/api/events?' + params({limit: 300}));
    S.items = data.items;
    S.sel.clear(); renderSel();
    $('count').textContent =
      `共 ${data.total} 条` + (data.items.length < data.total
        ? `，显示前 ${data.items.length} 条` : '');
    renderList();
  }catch(e){
    $('list').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }finally{
    $('list').classList.remove('loading');
  }
}

function pillClass(t){
  if(t==='重大变更') return 'pill major';
  if(t==='订单信号'||t==='中标') return 'pill order';
  if(t==='竞品命中') return 'pill hit';
  if(t==='深度分析') return 'pill hit';
  return 'pill';
}

// 收集当前生效的筛选条件（用于空结果提示与调试）
function activeFilters(){
  const out = [];
  if($('f-entity').value) out.push(`竞品＝${$('f-entity').value}`);
  if(S.signals.size)      out.push(`信号＝${[...S.signals].join('、')}`);
  if($('f-days').value && $('f-days').value !== 'all')
                          out.push(`时间＝${$('f-days').selectedOptions[0].textContent}`);
  if($('f-status').value) out.push(`状态＝${$('f-status').selectedOptions[0].textContent}`);
  if($('f-q').value)      out.push(`搜索＝${$('f-q').value}`);
  if(!$('f-lowrel').checked) out.push('仅高相关度');
  return out;
}

function clearFilters(){
  $('f-entity').value = '';
  $('f-q').value = '';
  $('f-status').value = '';
  S.signals.clear();
  $('f-days').value = TABS[S.tab].days;
  $('f-lowrel').checked = !!TABS[S.tab].lowRelDefault;
  renderSignalMenu();
  apply();
}

function renderList(){
  if(!S.items.length){
    const conds = activeFilters();
    $('list').innerHTML = `
      <div class="empty">
        <div style="font-size:15px;margin-bottom:8px">当前条件下没有记录</div>
        ${conds.length?`<div style="color:var(--dim);font-size:13px;margin-bottom:12px">
          生效的条件：${esc(conds.join(' · '))}</div>`:''}
        ${conds.length?`<button onclick="clearFilters()">清除全部筛选</button>`:''}
      </div>`;
    return;
  }
  $('list').innerHTML = S.items.map(e=>{
    const tags = (e.tags_list||[]).slice(0,6)
      .map(t=>`<span class="${pillClass(t)}">${esc(t)}</span>`).join('');
    const cls = ['row',
      e.status==='read'?'read':'',
      e.starred?'starred':'',
      (e.tags_list||[]).includes('重大变更')?'major':'',
      ((e.tags_list||[]).includes('订单信号')||(e.tags_list||[]).includes('中标'))?'order':''
    ].join(' ');
    const when = e.published_at || e.captured_at || '';

    // 右侧「提炼」栏：内容由后端按频道决定，无内容则不渲染该栏
    const hasBrief = !!(e.summary_long||'').trim();
    const briefLabel = e.brief_label || '提炼';
    const brief = hasBrief ? `
      <aside class="brief">
        <div class="brief-h">${esc(briefLabel)}</div>
        <div class="brief-b">${esc(e.summary_long)}</div>
      </aside>` : '';

    return `<div class="${cls} ${hasBrief?'two-col':''}" id="row-${e.id}">
      <input type="checkbox" onchange="toggleSel(${e.id}, this.checked)" ${S.sel.has(e.id)?'checked':''}>
      <div class="body">
        <div class="digest">${esc(e.digest)}</div>
        ${e.impact?`<div class="impact">↳ ${esc(e.impact)}</div>`:''}
        <div class="title-sub" title="点击「打开原文」查看全文">${esc(e.title)}</div>
        <div class="meta">
          <span class="pill">${esc(e.channel_label||'')}</span>
          ${e.entity?`<b>${esc(e.entity)}</b>`:''}
          <span>${esc(when)}</span>
          <span>${esc(e.source_name||'')}</span>
        </div>
        <div class="meta">${tags}</div>
        <div class="acts">
          ${e.report_id?`<button class="primary" onclick="openReport(${e.report_id},'${esc(e.entity||'')} 深度分析报告')">查看完整报告</button>`:''}
          ${e.url?`<a href="${esc(e.url)}" target="_blank" rel="noopener"><button>打开原文</button></a>`:''}
          <button onclick="toggleNote(${e.id})">${e.note?'编辑备注':'添加备注'}</button>
          <button onclick="patch(${e.id},{status:'${e.status==='read'?'new':'read'}'})">
            ${e.status==='read'?'标为未读':'标为已读'}</button>
          <button onclick="patch(${e.id},{starred:${e.starred?0:1}})">${e.starred?'取消星标':'加星'}</button>
          <button onclick="patch(${e.id},{status:'archived'})">归档</button>
        </div>
        <input class="note ${e.note?'show':''}" id="note-${e.id}" value="${esc(e.note||'')}"
               placeholder="备注（自动保存）"
               onblur="saveNote(${e.id}, this.value)"
               onkeydown="if(event.key==='Enter'){this.blur()}">
      </div>${brief}
    </div>`;
  }).join('');
}

// ═══════════════════════════════════════════════════════════
//  单项操作
// ═══════════════════════════════════════════════════════════
function toggleNote(id){
  const el = $('note-'+id);
  el.classList.toggle('show');
  if(el.classList.contains('show')) el.focus();
}

async function saveNote(id, val){
  await api('/api/update', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id, fields:{note: val}})});
}

async function patch(id, fields){
  await api('/api/update', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id, fields})});
  apply();
}

function toggleSel(id, on){
  on ? S.sel.add(id) : S.sel.delete(id);
  renderSel();
}

function renderSel(){
  const n = S.sel.size;
  $('selbar').classList.toggle('show', n>0);
  $('selcount').textContent = `已选 ${n} 条`;
  $('selinfo').textContent = n ? `已选 ${n} 条` : '';
}

function clearSel(){ S.sel.clear(); renderSel(); }

async function bulk(action){
  if(!S.sel.size) return;
  await api('/api/bulk', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ids:[...S.sel], action})});
  S.sel.clear(); renderSel(); apply();
}

async function markAllRead(){
  const ids = S.items.filter(e=>e.status==='new').map(e=>e.id);
  if(!ids.length) return alert('当前列表没有未读记录');
  if(!confirm(`把当前显示的 ${ids.length} 条标为已读？`)) return;
  await api('/api/bulk', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ids, action:'read'})});
  apply();
}

function exportCsv(){ window.location = '/api/export?' + params(); }
function reload(){ apply(); }

// ── 初始化 ────────────────────────────────────────────────
// 用 TABS 的默认值填充控件，然后首次加载。
// 注意：这里必须显式调用，否则页面会一片空白（所有渲染都由 apply() 触发）。
(function initUI(){
  const cfg = TABS[S.tab];
  $('f-days').value = cfg.days;
  $('f-lowrel').checked = !!cfg.lowRelDefault;
  $('f-sort').value = 'published_at:desc';
  document.querySelectorAll('.tab').forEach(el=>
    el.classList.toggle('active', el.dataset.tab === S.tab));
  renderSignalMenu();
  apply();
  // 每 60 秒自动刷新一次，保持数据新鲜
  setInterval(apply, 60000);
})();
</script>

<script>
function mdToHtml(md){
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const codes = [];
  md = md.replace(/```[\s\S]*?```/g, m => { codes.push(m); return `\u0000C${codes.length-1}\u0000`; });
  md = esc(md);
  md = md.replace(/`([^`\n]+)`/g, (m,c)=>`<code>${c}</code>`);
  md = md.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  md = md.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  const lines = md.split('\n');
  let out = [], inTable = false, inList = null, inQuote = false;
  const flush = () => {
    if (inTable){ out.push('</table>'); inTable = false; }
    if (inList){ out.push(`</${inList}>`); inList = null; }
    if (inQuote){ out.push('</blockquote>'); inQuote = false; }
  };
  for (const ln of lines){
    if (/^\|.*\|$/.test(ln)){
      flush();
      const cells = ln.slice(1,-1).split('|').map(c=>c.trim());
      if (cells.every(c=>/^:?-+:?$/.test(c))) continue;
      const isHead = out[out.length-1] === '<table>';
      if (!inTable){ out.push('<table>'); inTable = true; }
      out.push('<tr>' + cells.map(c=>`<${isHead?'th':'td'}>${c}</${isHead?'th':'td'}>`).join('') + '</tr>');
      continue;
    }
    if (/^\s*[-*]\s+/.test(ln)){
      if (inList !== 'ul'){ flush(); out.push('<ul>'); inList = 'ul'; }
      out.push('<li>' + ln.replace(/^\s*[-*]\s+/,'') + '</li>'); continue;
    }
    if (/^\s*\d+\.\s+/.test(ln)){
      if (inList !== 'ol'){ flush(); out.push('<ol>'); inList = 'ol'; }
      out.push('<li>' + ln.replace(/^\s*\d+\.\s+/,'') + '</li>'); continue;
    }
    if (/^>\s?/.test(ln)){
      if (!inQuote){ flush(); out.push('<blockquote>'); inQuote = true; }
      out.push('<p>' + ln.replace(/^>\s?/,'') + '</p>'); continue;
    }
    flush();
    const h = ln.match(/^(#{1,4})\s+(.*)$/);
    if (h){ out.push(`<h${h[1].length}>${h[2]}</h${h[1].length}>`); continue; }
    if (/^\s*---+\s*$/.test(ln)){ out.push('<hr>'); continue; }
    if (ln.trim()){ out.push('<p>' + ln + '</p>'); }
  }
  flush();
  let html = out.join('\n');
  html = html.replace(/\u0000C(\d+)\u0000/g, (m,i)=>`<pre>${esc(codes[+i])}</pre>`);
  return html;
}

async function openReport(id, title){
  $('reportTitle').textContent = title || '深度分析报告';
  $('reportBody').textContent = '加载中…';
  $('reportMask').classList.add('show');
  try {
    const r = await api('/api/report/' + id);
    if (r.error){ $('reportBody').textContent = '加载失败：' + r.error; return; }
    $('reportBody').innerHTML = mdToHtml(r.report || '');
  } catch(e){
    $('reportBody').textContent = '加载失败：' + e.message;
  }
}

function closeReport(){ $('reportMask').classList.remove('show'); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeReport(); });
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="竞品情报看板")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    db.init_db()  # 确保看板所需字段存在
    url = f"http://{args.host}:{args.port}/"

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"竞品情报看板已启动: {url}")
    print("按 Ctrl+C 停止")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
