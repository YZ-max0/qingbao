"""把 competitor-intel 的分析报告同步进自建情报系统。

数据来源：competitor-reports\*.md（由 run_batch_analysis.py 生成）。
说明：上游 history.db 的 analyses 表实际为空 —— 批量脚本只写 md 文件，
不写库（上游的 storage 只在 UI 流程里被调用）。因此这里以 md 文件为准。

写入目标：
    reports 表  —— 完整报告（长文本，单独存避免拖慢列表查询）
    events  表  —— 一条摘要级记录（channel='analysis'），让看板/告警能看到

幂等：按 (竞品名, 报告内容哈希) 去重，重复执行不产生重复记录。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from ..common import db
from ..common.util import setup_logging

log = setup_logging("report_sync")

# 深度分析报告的来源目录（由 competitor-intel 的批量脚本产出）。
# 默认取仓库内 third_party 同级的 competitor-reports 目录；
# 可用环境变量 REPORT_DIR 覆盖，避免换机器时要改代码。
REPORT_DIR = Path(os.environ.get("REPORT_DIR") or
                  (Path(__file__).resolve().parents[3].parent / "competitor-reports"))


def _parse_md(path: Path) -> dict | None:
    """解析一份报告 md，提取竞品名与我方产品。"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("读取失败 %s: %s", path.name, exc)
        return None

    if len(text) < 200:
        return None

    comp = path.stem                      # 文件名即竞品名
    m = re.search(r'^#\s*(.+?)\s*竞品分析报告', text, re.M)
    if m:
        comp = m.group(1).strip()

    our = ""
    m2 = re.search(r'我方产品[:：]\s*(.+)', text)
    if m2:
        our = m2.group(1).strip()

    return {"competitor": comp, "our_product": our, "report": text}


def _extract_strategy(report: str) -> str:
    """抽战略章节（SWOT / 威胁 / 行动），用于列表预览。"""
    m = re.search(
        r'(##[^\n]*(?:执行摘要|SWOT|竞争战略|威胁|行动)[\s\S]{0,4000})', report
    )
    return m.group(1)[:4000] if m else report[:4000]


def _extract_score(report: str) -> float | None:
    m = re.search(r'"?score"?\s*[:：]\s*(\d+(?:\.\d+)?)', report)
    return float(m.group(1)) if m else None


def run() -> dict[str, int]:
    db.init_db()
    stats = {"files": 0, "inserted": 0, "skipped": 0, "failed": 0}

    if not REPORT_DIR.exists():
        log.warning("报告目录不存在: %s", REPORT_DIR)
        return stats

    files = sorted(REPORT_DIR.glob("*.md"))
    log.info("发现 %d 份报告", len(files))

    for path in files:
        stats["files"] += 1
        item = _parse_md(path)
        if not item:
            stats["failed"] += 1
            continue

        # 内容哈希：报告更新后才重新入库
        digest = hashlib.sha256(item["report"].encode("utf-8")).hexdigest()[:32]

        with db.connect() as conn:
            # reports 表按 (competitor, hash) 去重
            exists = conn.execute(
                "SELECT id FROM reports WHERE competitor=? AND report_hash=?",
                (item["competitor"], digest),
            ).fetchone()
            if exists:
                stats["skipped"] += 1
                continue

            strategy = _extract_strategy(item["report"])
            score = _extract_score(item["report"])
            now = db.now_iso()

            cur = conn.execute(
                "INSERT INTO reports "
                "(competitor, our_product, report, strategy, score, "
                " report_hash, generated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item["competitor"], item["our_product"], item["report"],
                 strategy, score, digest, now),
            )
            rid = cur.lastrowid

            # events 里放一条，让看板与告警能看到
            title = f"{item['competitor']} 深度分析报告"
            conn.execute(
                "INSERT OR IGNORE INTO events "
                "(event_hash, source_type, source_name, channel, entity, title, "
                " summary, published_at, captured_at, relevance, tags) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    db.event_hash("competitor-intel", title, digest),
                    "analysis",
                    "competitor-intel",
                    "analysis",
                    item["competitor"],
                    title,
                    strategy[:400],
                    now,
                    now,
                    0.9,
                    json.dumps(["深度分析", "SWOT"], ensure_ascii=False),
                ),
            )
            stats["inserted"] += 1
            log.info("已同步 %s -> reports.id=%s (%d 字符)",
                     item["competitor"], rid, len(item["report"]))

    log.info("同步完成: %s", stats)
    return stats


if __name__ == "__main__":
    print(run())
