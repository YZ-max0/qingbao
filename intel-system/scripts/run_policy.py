"""采集政策栏目页（JS 渲染站点需要 playwright）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters import policy_adapter

if __name__ == "__main__":
    print(policy_adapter.run())
