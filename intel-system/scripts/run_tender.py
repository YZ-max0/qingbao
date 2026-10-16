"""采集招标 / 中标公告（中国政府采购网）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters import tender_adapter

if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(tender_adapter.run(days=days))
