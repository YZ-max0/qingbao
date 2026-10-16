"""用 LLM 为媒体稿等条目生成摘要与影响判断（可选，需 API key）。

未配置 OPENAI_API_KEY 时静默跳过，规则层摘要仍会在看板生效。

用法：
    python scripts/run_llm_digest.py [条数] [频道,频道]
    python scripts/run_llm_digest.py 200 media
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.report import llm

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    chans = sys.argv[2].split(",") if len(sys.argv) > 2 else ["media"]
    print(llm.run(limit=limit, channels=chans))
