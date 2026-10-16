"""解析中标公告详情页，抓出中标供应商（让"竞品命中"有数据）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters import award_adapter

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    print(award_adapter.run(limit=limit))
