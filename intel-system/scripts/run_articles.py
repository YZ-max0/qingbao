"""抓取情报条目的正文全文（供 LLM 提炼使用）。

用法：
    python scripts/run_articles.py [条数] [频道]
    python scripts/run_articles.py 100 media
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters import article_adapter

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    chans = tuple(sys.argv[2].split(",")) if len(sys.argv) > 2 else ("media",)
    print(article_adapter.fetch_bodies(limit=limit, channels=chans))
