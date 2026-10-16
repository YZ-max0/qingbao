"""采集上市公司公告（API 通路，AkShare）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters import announcement_adapter

if __name__ == "__main__":
    begin = sys.argv[1] if len(sys.argv) > 1 else "2026-01-01"
    print(announcement_adapter.run(begin_date=begin))
