"""生成日报。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.report import daily

if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    path = daily.run(days=days)
    print(Path(path).read_text(encoding="utf-8"))
