"""初始化数据库。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.db import init_db
from src.common.util import DB_PATH


if __name__ == "__main__":
    init_db()
    print(f"数据库已初始化: {DB_PATH}")
