"""把 competitor-intel 的分析报告同步进自建情报系统（看板可见）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters import report_sync_adapter

if __name__ == "__main__":
    print(report_sync_adapter.run())
