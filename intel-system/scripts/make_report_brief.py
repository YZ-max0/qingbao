"""给深度分析报告生成要点提炼（供看板右侧栏展示）。

幂等：只处理尚无 brief 的报告，重复执行不会重复消耗 LLM 调用。

用法：
    python scripts/make_report_brief.py          # 处理全部待提炼的
    python scripts/make_report_brief.py 10       # 最多处理 10 份
    python scripts/make_report_brief.py --redo   # 强制重做（改提示词后用）
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.report import report_brief

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("limit", nargs="?", type=int, default=100)
    ap.add_argument("--redo", action="store_true")
    a = ap.parse_args()
    print(report_brief.run(limit=a.limit, redo=a.redo))
