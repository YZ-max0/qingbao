"""中标公告详情页解析 —— 抓出「中标供应商」，让竞品命中真正有数据。

背景：政采列表页只有"采购人 + 代理机构"，中标厂商藏在详情页的表格里。
不解析详情页，就永远只能知道"有人在买"，不知道"谁卖了"。

实测（2026-10）详情页表格结构：
    序号 | 中标（成交）金额(元) | 中标供应商名称 | 中标供应商地址 | 评审总得分
    1    | 总价：492000（元）   | 苏州市伏泰信息科技股份有限公司 | ... | 94.95

合规：ccgp 无有效 robots.txt（返回 404 HTML 错误页），属公开页面；
抓取仍保持低频 + 随机延迟。
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..common import db
from ..common.fetcher import Fetcher
from ..common.scoring import match_competitor
from ..common.util import CONFIG_DIR, load_yaml, setup_logging

log = setup_logging("award")

# 详情页之间请求的最小间隔（秒）：低频、礼貌
MIN_INTERVAL = 3.0


def _cell_text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True))


def _clean_name(name: str) -> str:
    """清理抽取出供应商名的常见噪声尾巴。

    版式C 会把后续字段并进来（如「广州星樾智教科技有限公司 供应商联系人」），
    括号里的包件号也不属于公司名本体，统一剥掉。
    """
    n = re.sub(r"\s+", " ", name or "").strip()
    # 截断到第一个明显的字段标签
    n = re.split(
        r"(?:供应商联系人|供应商地址|联系人|联系电话|详见附件|标包|货物|中标)",
        n,
    )[0].strip(" ：:、，,")
    # 去掉包件后缀："...有限公司（包件2" / "...（第二包）"
    n = re.sub(r"[（(]\s*包件.*$", "", n).strip(" ：:、，,")
    return n


def _looks_like_company(name: str) -> bool:
    """判断单元格是否真的是公司名。

    版式C 的页面会把"供应商名称 ：XXX"整串塞进单元格，导致表头行本身
    就是数据行，且后续每个字段都被当成一个"供应商"。用形态学规则过滤：
    真公司名不长、不含冒号、且不以"详见附件"之类的占位语结尾。
    """
    if not name:
        return False
    if "：" in name or ":" in name:      # 含标签冒号 => 是 "键：值" 混排
        return False
    if len(name) > 40 or len(name) < 4:
        return False
    if any(w in name for w in ("详见附件", "标包", "金额", "地址", "品牌", "型号", "数量", "单价")):
        return False
    # 公司名常见后缀
    return any(w in name for w in (
        "公司", "集团", "研究院", "研究所", "学院", "大学", "中心", "厂", "社", "企业",
    ))


def parse_award(html: str) -> dict:
    """从中标公告详情页解析出结构化字段。"""
    soup = BeautifulSoup(html, "lxml")
    out: dict = {"suppliers": [], "project": "", "buyer": "", "amount": ""}

    # 1) 项目名 / 采购单位：来自"公告信息"表格的键值行
    for tb in soup.find_all("table"):
        for tr in tb.find_all("tr"):
            cells = [_cell_text(td) for td in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            k, v = cells[0], cells[1]
            if k == "采购项目名称" and not out["project"]:
                out["project"] = v
            elif k == "采购单位" and not out["buyer"]:
                out["buyer"] = v

    # 2) 中标供应商：ccgp 详情页有两种表格版式，都要支持
    #    版式A（旧）：序号 | 中标（成交）金额(元) | 中标供应商名称 | 地址 | 得分
    #    版式B（新）：供应商名称 | 供应商地址 | 评审方法 | 是否价格扣除 | 中标（成交）金额 | 得分
    #    另有"货物明细"表（品目号/采购标的/品牌/规格型号），供应商列在第二列，
    #    该表不含金额汇总，仅作补充，优先取版式 A/B。
    for tb in soup.find_all("table"):
        rows = tb.find_all("tr")
        if not rows:
            continue
        header = [_cell_text(th) for th in rows[0].find_all(["td", "th"])]
        if not header:
            continue

        # 定位含"供应商名称"的列（两种版式都含该字样）
        i_name = next(
            (i for i, h in enumerate(header)
             if "供应商名称" in h or "中标供应商" in h),
            None,
        )
        if i_name is None:
            continue
        # 排除"货物明细"表：它没有金额汇总列
        if not any("金额" in h for h in header):
            continue

        i_amt = next((i for i, h in enumerate(header) if "金额" in h), None)

        for tr in rows[1:]:
            cells = [_cell_text(td) for td in tr.find_all(["td", "th"])]
            if len(cells) <= i_name:
                continue
            name = _clean_name(cells[i_name])
            if not _looks_like_company(name):
                continue
            amount = cells[i_amt].strip() if (i_amt is not None and len(cells) > i_amt) else ""
            # 金额列可能取到非数值内容（版式C 的键值混排），只接受含数字的
            if amount and not re.search(r"\d", amount):
                amount = ""
            out["suppliers"].append({"name": name, "amount": amount})
            if not out["amount"] and amount:
                out["amount"] = amount
        if out["suppliers"]:
            break

    # 3) 版式C：纵向"键：值"排版，没有标准表格
    #    如 "供应商名称 ：山东荣沃教育科技有限公司" / "中标 金额：2130000.00 元"
    #    值必须在遇到下一个"标签："之前结束，否则会把后续字段全吞进来。
    if not out["suppliers"]:
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        # 值中不允许出现"："或"："，这样天然在下一个标签处截断
        names = re.findall(
            r"(?:中标)?供应商名称\s*[:：]\s*([^:：]{4,60})",
            text,
        )
        for nm in names:
            nm = _clean_name(nm)
            if nm and 4 <= len(nm) <= 40 and _looks_like_company(nm):
                out["suppliers"].append({"name": nm, "amount": ""})
        if out["suppliers"] and not out["amount"]:
            amt = re.search(r"中标\s*金额\s*[:：]\s*[（(]?[^)）]*?([\d,]+(?:\.\d+)?)", text)
            if amt:
                out["amount"] = amt.group(1)

    return out


def _norm_amount(raw: str) -> str:
    """'总价：492000（元）' -> '492000元'。

    只接受含数字的值；版式C 的键值混排会把公司名塞进金额列，
    这类非数值一律丢弃，避免污染汇总。
    """
    if not raw or not re.search(r"\d", raw):
        return ""
    m = re.search(r"([\d,]+(?:\.\d+)?)", raw)
    if not m:
        return ""
    num = m.group(1).replace(",", "").rstrip(".")
    return f"{num}元"


def pending_awards(limit: int = 40) -> list[dict]:
    """取尚未解析过中标供应商的中标公告。"""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, url, tags FROM events
            WHERE channel='tender'
              AND (tags LIKE '%中标%' OR tags LIKE '%成交%')
              AND url IS NOT NULL AND url <> ''
              AND (tags NOT LIKE '%已解析%')
            ORDER BY published_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def run(limit: int = 40) -> dict[str, int]:
    import time

    cfg = load_yaml(CONFIG_DIR / "sources.yaml")
    policy = cfg.get("crawl_policy", {}) or {}
    fetcher = Fetcher(
        delay_range=tuple(policy.get("delay_seconds", [3, 6])),
        respect_robots=bool(policy.get("respect_robots", True)),
    )

    stats = {"checked": 0, "parsed": 0, "supplier_found": 0,
             "competitor_hit": 0, "failed": 0}
    items = pending_awards(limit)
    log.info("待解析中标公告: %d 条", len(items))

    with db.connect() as conn:
        run_id = db.start_run(conn, "award_detail")
        for n, it in enumerate(items):
            if n:
                time.sleep(MIN_INTERVAL)
            resp = fetcher.get(it["url"])
            stats["checked"] += 1
            if resp is None:
                stats["failed"] += 1
                continue
            resp.encoding = resp.apparent_encoding or "utf-8"

            info = parse_award(resp.text)
            if not info["suppliers"]:
                # 解析不到就标记，避免每次重复抓取
                conn.execute(
                    "UPDATE events SET tags=? WHERE id=?",
                    (_add_tag(it["tags"], "已解析-无供应商"), it["id"]),
                )
                continue

            stats["parsed"] += 1
            stats["supplier_found"] += len(info["suppliers"])

            names = [s["name"] for s in info["suppliers"]]
            amount = _norm_amount(info["amount"])

            # 竞品识别：中标厂商名里是否出现竞品
            hit_name, hit_tier = None, None
            for nm in names:
                e, t = match_competitor(nm)
                if e:
                    hit_name, hit_tier = e, t
                    break

            summary = (
                f"中标供应商: {'、'.join(names)}"
                + (f" | 金额: {amount}" if amount else "")
                + (f" | 采购单位: {info['buyer']}" if info["buyer"] else "")
            )

            tags = _add_tag(it["tags"], "已解析")
            if hit_name:
                tags = _add_tag(tags, "竞品命中")
                stats["competitor_hit"] += 1
                log.info("🎯 竞品命中: %s 中标 %s", hit_name, info["project"] or it["title"][:30])

            conn.execute(
                "UPDATE events SET summary=?, tags=?, entity=COALESCE(entity,?), "
                "entity_tier=COALESCE(entity_tier,?), updated_at=? WHERE id=?",
                (summary, tags, hit_name, hit_tier, db.now_iso(), it["id"]),
            )

        db.finish_run(conn, run_id, stats["checked"], stats["parsed"],
                      stats["checked"] - stats["parsed"])
    log.info("中标详情解析完成: %s", stats)
    return stats


def _add_tag(tags_json: str | None, tag: str) -> str:
    import json

    try:
        tags = json.loads(tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    if tag not in tags:
        tags.append(tag)
    return json.dumps(sorted(set(tags)), ensure_ascii=False)
