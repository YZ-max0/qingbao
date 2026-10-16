"""推送告警：只发新出现的高价值信号。

用法：
  python scripts/send_alerts.py           # 实际推送
  python scripts/send_alerts.py --dry-run # 只预览，不发送、不记录
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.report import alert

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    print(alert.run(dry_run=dry))
