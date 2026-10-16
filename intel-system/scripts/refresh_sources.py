"""每日增量：为已有分析的竞品补充最新搜索来源。

背景：
    来源回补原本只在手动跑批量分析时发生。但 competitor-intel 用的是
    Tavily 搜索，**每天都会有新结果**（新闻、公告、新品）。若只在手动
    跑时才回补，看板上的"分析来源"会逐渐过时。

    本脚本对「已分析过但来源较少」的竞品做增量分析，把新来源回补进库。
    幂等：相同 URL 自动去重，不会重复插入。

用法：
    python scripts/refresh_sources.py            # 默认处理 3 个
    python scripts/refresh_sources.py 5          # 处理 5 个
    python scripts/refresh_sources.py 5 --days 30  # 只看 30 天内的新来源（更快）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common import db
from src.common.util import setup_logging

log = setup_logging("refresh_sources")

# 伴生项目 competitor-intel 的位置。
# 默认取仓库内 third_party/competitor-intel；可用环境变量 COMPETITOR_INTEL_DIR
# 覆盖（例如你把它 clone 到了别处）。
_HERE = Path(__file__).resolve()
COMPETITOR_INTEL = Path(os.environ.get("COMPETITOR_INTEL_DIR") or
                        (_HERE.parents[2] / "third_party" / "competitor-intel"))
PY = sys.executable


def pick_targets(limit: int) -> list[str]:
    """挑「来源较少」的竞品优先刷新。

    注意排除"来源为 0"的竞品：实测这类通常是 Tavily 标题里不含其精确
    名称（如 CSG华晓 可能写作 CSG），反复刷新也拿不到来源，白白消耗配额。
    它们的报告已经生成，无需重复分析。

    另外优先刷新「较久未更新」的，避免每天重复分析同一家。
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT entity AS name, COUNT(*) AS n, MAX(captured_at) AS last
            FROM events
            WHERE channel = 'analysis_src' AND entity IS NOT NULL
            GROUP BY entity
            """
        ).fetchall()
        stats = {r["name"]: (r["n"], r["last"] or "") for r in rows}

        reps = [r["competitor"] for r in conn.execute(
            "SELECT DISTINCT competitor FROM reports"
        )]

    # 只考虑"至少有过 1 条来源"的竞品；按来源数升序 + 最近刷新时间升序
    cand = [n for n in reps if stats.get(n, (0, ""))[0] > 0]
    cand.sort(key=lambda n: (stats[n][0], stats[n][1]))
    return cand[:limit]


def run_batch(names: list[str]) -> int:
    """调用 competitor-intel 的批量分析（它会自动回补来源）。"""
    if not names:
        log.info("没有需要刷新的竞品")
        return 0

    log.info("刷新 %d 个竞品的来源: %s", len(names), "、".join(names))
    cmd = [PY, str(COMPETITOR_INTEL / 'run_batch_analysis.py'), *names]
    log.info("执行: %s", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(COMPETITOR_INTEL),
            timeout=3000,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
    except subprocess.TimeoutExpired:
        log.warning("刷新超时（50 分钟），已中止")
        return 1
    except Exception as exc:  # noqa: BLE001
        log.warning("调用失败: %s", exc)
        return 1

    tail = (proc.stdout or '')[-1500:]
    log.info("退出码 %s\n%s", proc.returncode, tail)
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("limit", nargs="?", type=int, default=3,
                    help="本次刷新几个竞品（默认 3，控制每日耗时）")
    args = ap.parse_args()

    db.init_db()
    targets = pick_targets(args.limit)
    if not targets:
        log.info("库里还没有分析报告，跳过来源刷新")
        return 0

    return run_batch(targets)


if __name__ == "__main__":
    raise SystemExit(main())
