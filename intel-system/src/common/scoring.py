"""相关度打分与竞品识别：把噪音过滤在入库之前。

两套词表，因为两类内容的语汇完全不同：
  - 招标（tender_keywords.yaml）：品类词 = "仓储机器人/AMR/AGV"
  - 政策（policy_keywords.yaml）：政策语汇 = "制造业/物流/新质生产力"
若政策复用招标词表，实测会导致所有政策被过滤（21 条全丢）。
"""
from __future__ import annotations

from .util import CONFIG_DIR, load_yaml

_cfg: dict | None = None
_policy_cfg: dict | None = None


def cfg() -> dict:
    """读取招标关键词配置，并把嵌套结构拍平为顶层 key。

    tender_keywords.yaml 用 category_keywords / scene_keywords 分组存储，
    这里统一拍平，避免调用方关心嵌套层级。
    """
    global _cfg
    if _cfg is None:
        raw = load_yaml(CONFIG_DIR / "tender_keywords.yaml")
        cat = raw.get("category_keywords", {}) or {}
        flat = dict(raw)
        flat["core"] = cat.get("core", []) or []
        flat["weak"] = cat.get("weak", []) or []
        flat["noise"] = cat.get("noise", []) or []
        flat["scene"] = raw.get("scene_keywords", []) or []
        _cfg = flat
    return _cfg


def policy_cfg() -> dict:
    """政策专用词表。"""
    global _policy_cfg
    if _policy_cfg is None:
        _policy_cfg = load_yaml(CONFIG_DIR / "policy_keywords.yaml")
    return _policy_cfg


def _flat_competitors() -> dict[str, tuple[str, str]]:
    """返回 {匹配串: (正式名, 梯队)}。

    配置支持两种写法（兼容旧格式）：
        tier1: [A, B]                    -> 列表，每项各自成为主体
        tier1: {A: [别名1, 别名2], B: []} -> 映射，正式名 + 别名归并到同一主体

    映射写法是推荐格式：避免把同一家公司的全称与简称当成两个主体，
    导致重复分析、重复消耗搜索配额。

    过滤规则：短于 3 字符的中文串必须出现在别名表中才参与匹配 ——
    "极智""快仓"这类词不校验会误命中无关文本。
    """
    out: dict[str, tuple[str, str]] = {}
    names = cfg().get("competitor_names", {})

    for tier, items in (names or {}).items():
        if isinstance(items, dict):
            # 推荐格式：{正式名: [别名...]}
            for full, aliases in items.items():
                full = str(full).strip()
                if not full:
                    continue
                out[full] = (full, tier)
                for a in (aliases or []):
                    a = str(a).strip()
                    if a and a != full:
                        out[a] = (full, tier)
        elif isinstance(items, list):
            # 旧格式：每项独立成主体
            for raw in items:
                s = str(raw).strip()
                if s:
                    out[s] = (s, tier)

    # 去掉会误命中的过短匹配串（2 字以内且非已知品牌别名）
    return {
        k: v for k, v in out.items()
        if len(k) >= 3 or (len(k) == 2 and all(
            "\u4e00" <= ch <= "\u9fff" for ch in k))
    }


def match_competitor(text: str, exclude: str | None = None) -> tuple[str | None, str | None]:
    """从文本中识别竞品，返回 (正式名, 梯队)。

    exclude: 当前记录自身所属主体。公司公告/自家稿件里必然出现自己的名字，
    那不是"竞品命中"，必须排除，否则会把自有信息误判成竞品动态。
    """
    hits = _all_hits(text)
    if not hits:
        return None, None
    if exclude:
        others = [h for h in hits if h[0] != exclude]
        if not others:
            return None, None
        hits = others
    return hits[0]


def _all_hits(text: str) -> list[tuple[str, str]]:
    """返回文本命中的全部竞品，按匹配串长度降序（长名优先，避免短名抢先）。"""
    found: dict[str, str] = {}
    for alias, (full, tier) in _flat_competitors().items():
        if alias in text and full not in found:
            found[full] = tier
    # 长匹配串优先：如"快仓智能"应先于"快仓"决定归属
    ordered = sorted(
        found.items(),
        key=lambda kv: -max(
            (len(a) for a, (f, _) in _flat_competitors().items() if f == kv[0]),
            default=0,
        ),
    )
    return ordered


def score_relevance(title: str, content: str = "", kind: str = "tender") -> float:
    """相关度打分 0~1：品类词强匹配、场景词加成、噪音词一票否决。

    kind: "tender" 用招标词表；"policy" 用政策词表。
    """
    c = policy_cfg() if kind == "policy" else cfg()
    text = f"{title} {content}"
    title_l = title.lower()

    for noise in c.get("noise", []) or []:
        if noise.lower() in text.lower():
            return 0.0

    score = 0.0
    core_hits = [k for k in c.get("core", []) or [] if k.lower() in text.lower()]
    weak_hits = [k for k in c.get("weak", []) or [] if k.lower() in text.lower()]
    scene_hits = [k for k in c.get("scene", []) or [] if k.lower() in text.lower()]

    if core_hits:
        score += 0.6
        # 标题命中权重更高：标题是招标/新闻的核心信号
        if any(k.lower() in title_l for k in core_hits):
            score += 0.2
    if weak_hits and scene_hits:
        score += 0.3
    elif weak_hits:
        score += 0.1
    if len(core_hits) >= 2:
        score += 0.1

    entity, _ = match_competitor(text)
    if entity:
        score += 0.2

    return round(min(score, 1.0), 2)


def keyword_hits(text: str, kind: str = "tender") -> list[str]:
    """返回命中的关键词，写入 tags 便于后续检索。"""
    c = policy_cfg() if kind == "policy" else cfg()
    return [k for k in (c.get("core", []) or []) if k.lower() in text.lower()]
