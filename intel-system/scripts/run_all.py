"""一键运行全部采集 + 提炼 + 日报 + 告警。

执行顺序（依赖关系不可颠倒）：
    采集 → 正文抓取 → LLM 提炼 → 日报 → 告警
正文必须在提炼之前，提炼必须在日报/告警之前，否则推送出去的还是旧摘要。

容错：
    - 政策源需 playwright + chromium，缺失时自动跳过
    - LLM 未配置 API key 时自动降级为规则摘要，不影响其余链路
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters import (
    announcement_adapter,
    article_adapter,
    award_adapter,
    finance_adapter,
    media_adapter,
    policy_adapter,
    site_change_adapter,
    tender_adapter,
)
from src.common.db import init_db
from src.report import alert, daily, llm


if __name__ == "__main__":
    init_db()

    print("== 1. 上市公告（API 通路，最稳） ==")
    print(announcement_adapter.run(begin_date="2026-01-01"))

    print("== 2. 股本变动 / 融资信号（API 通路） ==")
    print(finance_adapter.run())

    print("== 3. 行业媒体（新品 / 融资 / 大单） ==")
    print(media_adapter.run())

    print("== 4. 招标/中标（爬虫通路，限频） ==")
    print(tender_adapter.run(days=30))

    print("== 5. 中标详情页（抓中标供应商，识别竞品） ==")
    print(award_adapter.run(limit=40))

    print("== 6. 官网变更（爬虫通路） ==")
    site_change_adapter.sync_targets_from_config()
    print(site_change_adapter.check_targets(only_tier1=True))

    print("== 7. 政策栏目（需 chromium） ==")
    print(policy_adapter.run())

    # ── 提炼链路：先有正文，才能提炼 ──
    print("== 8. 抓正文（供提炼使用） ==")
    print(article_adapter.fetch_bodies(limit=120, channels=("media",)))

    print("== 9. LLM 提炼（未配置 key 时自动降级） ==")
    print(llm.run(limit=150, channels=("media",)))

    print("== 10. 日报 ==")
    print(daily.run(days=7))

    print("== 11. 告警（只推新信号，未配置渠道则跳过） ==")
    print(alert.run())
