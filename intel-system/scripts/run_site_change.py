"""官网变更监控：先同步监控目标，再检查变化。

首次运行只建立基线，第二次起才会产出 site_change 事件。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters import site_change_adapter

if __name__ == "__main__":
    only_tier1 = "--all" not in sys.argv
    site_change_adapter.sync_targets_from_config()
    print(site_change_adapter.check_targets(only_tier1=only_tier1))
