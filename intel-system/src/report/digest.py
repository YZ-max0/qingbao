"""情报提炼层 —— 把原始字段变成"扫一眼就懂"的一句话。

问题：入库的 summary 要么为空（行业媒体 243 条全空），要么是把字段拼起来
（"变动日期: X | 公告日期: Y | 总股本: Z"），读者仍需自己解读。

目标：每条记录给出一句人话，回答三个问题
    谁 → 做了什么 → 结果/金额（对我意味着什么）

两层实现：
    1. 规则层（本模块）：用已有结构化字段拼装，零成本、不编造，覆盖招投标/公告/股本
    2. LLM 层（见 llm.py）：处理规则拼不出的（如纯标题的媒体报道），可选用
"""
from __future__ import annotations

import json
import re

# ── 工具 ────────────────────────────────────────────────────

def _tags(row: dict) -> list[str]:
    try:
        return json.loads(row.get("tags") or "[]")
    except json.JSONDecodeError:
        return []


def _money(raw: str) -> str:
    """492000元 -> 49.2 万元；1200000元 -> 120 万元。"""
    m = re.search(r"([\d.]+)", raw or "")
    if not m:
        return ""
    try:
        v = float(m.group(1))
    except ValueError:
        return ""
    if v >= 1e8:
        return f"{v / 1e8:.2f} 亿元"
    if v >= 1e4:
        return f"{v / 1e4:.1f} 万元".replace(".0 ", " ")
    return f"{v:.0f} 元"


def _clean_company(name: str) -> str:
    """去掉公司名里的地名/后缀噪音，让摘要更短。"""
    n = (name or "").strip()
    n = re.sub(r"^(.{2,8}?[省市])", "", n)          # 去掉"苏州市"
    n = re.sub(r"(股份有限公司|有限责任公司|有限公司|公司)$", "", n)
    return n or (name or "").strip()


def _clean_project(title: str) -> str:
    """从标题里抽项目名，去掉代理机构前缀与公告类型后缀。"""
    t = re.sub(r"^.{2,40}?(有限公司|有限责任公司|咨询集团|招标代理|采购中心)关于", "", title or "")
    t = re.sub(r"(公开招标公告|招标公告|中标(（成交）|\(成交\))?结果公告|中标公告|采购公告|结果更正公告.*|的公开招标.*)$", "", t)
    # 去掉项目编号等噪音
    t = re.sub(r"[（(]\s*项目编号[:：][^)）]*[)）]", "", t)
    t = re.sub(r"\s*[-—]+\s*(新一代|第[一二三四五六七八九十]次).*$", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip(" ：:、-—")[:80] or (title or "")[:80]


def _who(entity: str | None, source: str) -> str:
    """归属主体：竞品名优先，否则从来源名推断。"""
    if entity:
        return entity
    m = re.match(r"(.+?)(上市公告|股本变动|官网)", source or "")
    return m.group(1) if m else ""


# ── 各频道模板 ──────────────────────────────────────────────

def _tender(row: dict) -> str:
    """招投标：谁 以多少 中标了 什么项目（哪个单位采购）。"""
    title = row.get("title") or ""
    summary = row.get("summary") or ""
    tags = _tags(row)

    proj = _clean_project(title)
    sup = ""
    amount = ""
    buyer = ""
    if "中标供应商" in summary:
        part = next((p for p in summary.split("|") if "中标供应商" in p), "")
        sup = part.replace("中标供应商:", "").strip()
        amt = next((p for p in summary.split("|") if "金额" in p), "")
        amount = _money(amt.replace("金额:", ""))
        by = next((p for p in summary.split("|") if "采购单位" in p), "")
        buyer = by.replace("采购单位:", "").strip()

    is_win = "中标" in tags or "成交" in title
    if is_win and sup:
        line = f"{_clean_company(sup)} 中标"
        if amount:
            line += f"（{amount}）"
        line += f"：{proj}"
        if buyer:
            line += f"｜采购方：{_clean_company(buyer)}"
        return line
    if is_win:
        line = f"中标结果：{proj}"
        if buyer:
            line += f"｜采购方：{_clean_company(buyer)}"
        return line
    # 招标公告 = 需求刚发布
    line = f"采购需求：{proj}"
    if buyer:
        line += f"｜采购方：{_clean_company(buyer)}"
    return line


def _announcement(row: dict) -> str:
    """上市公告：把"关于X的公告"翻译成行动描述。"""
    title = row.get("title") or ""
    tags = _tags(row)
    name = _who(row.get("entity"), row.get("source_name"))
    body = re.sub(r"^[^:：]{2,8}[:：]", "", title)
    # 剥离公文腔的冗余词，只留动作本身
    body = re.sub(r"^关于", "", body)
    body = body.replace("的公告", "").replace("的进展", "").replace("公告", "")
    body = body.replace("暨", "，").strip("，。 ")

    if "重大变更" in tags:
        return f"【重大】{name} {body.split('，')[0]}"
    if "订单信号" in tags:
        return f"【订单】{name} {body.split('，')[0]}"
    # 按公告内容归类
    if any(k in body for k in ("回购",)):
        return f"{name} 回购股份：{body.split('，')[0]}"
    if any(k in body for k in ("增持", "减持")):
        return f"{name} 股东{('增持' if '增持' in body else '减持')}：{body.split('，')[0]}"
    if any(k in body for k in ("激励", "授予")):
        return f"{name} 股权激励：{body.split('，')[0]}"
    if any(k in body for k in ("投资", "增资")):
        return f"{name} 对外投资：{body.split('，')[0]}"
    if any(k in body for k in ("董事会", "监事会", "决议")):
        return f"{name} 公司治理：{body.split('，')[0]}"
    return f"{name} {body.split('，')[0]}"


def _finance(row: dict) -> str:
    """股本变动：讲清"发生了什么"，而非罗列字段。"""
    title = row.get("title") or ""
    summary = row.get("summary") or ""
    name = _who(row.get("entity"), row.get("source_name"))
    reason = next(
        (p.replace("变动原因:", "").strip() for p in summary.split("|") if "变动原因" in p),
        title.replace(f"{name} 股本变动：", ""),
    )
    total = ""
    m = re.search(r"总股本:\s*([\d.]+)", summary)
    if m:
        try:
            total = f"，总股本 {float(m.group(1)) / 1e4:.2f} 亿股"
        except ValueError:
            total = ""

    if "增发" in reason or "募集" in reason:
        return f"{name} 增发新股{total} —— 融资动作，资金到位"
    if "激励" in reason:
        return f"{name} 实施股权激励{total} —— 绑定核心团队"
    if "回购" in reason:
        return f"{name} 回购股份{total} —— 维护股价"
    if "定期报告" in reason:
        return f"{name} 定期报告披露{total}"
    return f"{name} 股本变动：{reason}{total}"


def _media(row: dict) -> str:
    """媒体稿：标题本身已是完整句子，补上"这是什么类型的事"。"""
    title = (row.get("title") or "").strip()
    tags = _tags(row)
    kinds = [k for k in ("融资动态", "新品发布", "订单信号") if k in tags]
    if "竞品命中" in tags and row.get("entity"):
        prefix = f"【竞品·{row['entity']}】"
    elif kinds:
        prefix = f"【{kinds[0]}】"
    else:
        prefix = ""
    return f"{prefix}{title}"


def _policy(row: dict) -> str:
    title = (row.get("title") or "").strip()
    return f"【政策】{title}"


def _default(row: dict) -> str:
    return (row.get("title") or "").strip()


# ── 「对我意味着什么」：基于标签 + 频道的规则判断 ─────────────
# LLM 未配置时由规则兜底；LLM 可用时其结论优先（见 dashboard 的字段选择）。
#
# 每项 = (标签关键词, 结论, 证据要求)
#   证据要求用于避免"零信息给结论"：
#     "any"   —— 有该标签即可
#     "win"   —— 需要解析出中标供应商/金额，否则结论待确认
_IMPACT_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("重大变更",), "对手股权/控制权生变，战略与渠道可能调整，优先关注", "any"),
    (("融资动态", "融资信号"), "对手拿到资金，可能扩产或打价格战", "any"),
    (("增资", "增发"), "对手资金到位，产能与市场投入可能加大", "any"),
    (("激励",), "对手绑定核心团队，人员稳定性增强", "any"),
    (("回购",), "对手维护股价，多为信心信号", "any"),
    (("订单信号", "中标"), "已有厂商拿下该单，此项目机会窗口关闭", "win"),
    (("签约",), "对手落单，关注其客户行业与方案构成", "any"),
    (("招标",), "采购需求刚发布，是可直接跟进的线索", "any"),
    (("新品发布",), "对手推出新品，需对比自身产品差距", "any"),
    (("竞品命中",), "信息直接涉及竞品，注意其动作方向", "any"),
]

# 媒体稿的「订单信号」含义不同：它报道的是行业整体订单情况，
# 而不是某个具体项目被谁拿下。用同一条结论会误导读者。
_MEDIA_ORDER_IMPACT = "行业订单动向，可据此判断市场景气与竞争强度"
_MEDIA_FINANCE_IMPACT = "对手拿到资金，可能扩产或打价格战"
_MEDIA_NEWPRODUCT_IMPACT = "对手推出新品，需对比自身产品差距"


def _has_win_evidence(row: dict) -> bool:
    """中标类结论是否具备证据：解析出了供应商或金额。

    仅凭标题含"中标"二字就断言"已有厂商拿下该单"，在详情页解析失败时
    会给出读者无法核实的结论。这里要求更硬的证据。
    """
    s = row.get("summary") or ""
    if "中标供应商" in s:
        # summary 形如 "中标供应商: X | 金额: Y"，需确认字段非空
        part = next((p for p in s.split("|") if "中标供应商" in p), "")
        if part.replace("中标供应商:", "").strip():
            return True
    if re.search(r"金额\s*[:：]\s*\d", s):
        return True
    return False


def impact(row: dict) -> str:
    """给出一句"这条对我意味着什么"。

    设计约束（依据实测修订）：
      1. **零信息不产出结论**：没有标题的记录直接返回空（此前会对空记录
         输出"行业动态，用于把握整体风向"）。
      2. **媒体稿与政采稿分开判断**：媒体稿的「订单信号」是指行业订单动向
         （如"7家企业手握近百亿订单"），并非某个具体项目被谁拿下。用同一条
         结论会误导读者，因此按频道使用不同措辞。
      3. **证据不足时标注待确认**：政采中标类结论若未解析出供应商/金额，
         追加"（未解析到供应商/金额，结论待确认）"。
    """
    # 约束 1：无标题即无任何可依据的信息
    if not (row.get("title") or "").strip():
        return ""

    tags = _tags(row)
    channel = row.get("channel") or ""

    # 约束 2：媒体稿另用一套措辞
    if channel == "media":
        if any(t in tags for t in ("融资动态", "融资信号")):
            return _MEDIA_FINANCE_IMPACT
        if "新品发布" in tags:
            return _MEDIA_NEWPRODUCT_IMPACT
        if any(t in tags for t in ("订单信号", "中标", "签约")):
            return _MEDIA_ORDER_IMPACT
        if "竞品命中" in tags:
            return "信息直接涉及竞品，注意其动作方向"
        if "重大变更" in tags:
            return "行业格局变动，关注对自身定位的影响"
        return "行业动态，用于把握整体风向"

    for words, text, need in _IMPACT_RULES:
        if not any(w in tags for w in words):
            continue
        # 约束 3：政采中标结论需有供应商/金额支撑
        if need == "win" and not _has_win_evidence(row):
            return f"{text}（未解析到供应商/金额，结论待确认）"
        return text

    if channel == "policy":
        return "行业政策风向，可能影响招投标与补贴环境"
    return ""


BUILDERS = {
    "tender": _tender,
    "announcement": _announcement,
    "finance": _finance,
    "media": _media,
    "policy": _policy,
}


def build(row: dict) -> str:
    """按频道选择模板生成读得懂的摘要。"""
    fn = BUILDERS.get(row.get("channel") or "", _default)
    try:
        out = fn(row)
    except Exception:  # noqa: BLE001 — 提炼失败不能影响主流程
        out = row.get("title") or ""
    return re.sub(r"\s+", " ", out).strip()[:300]
