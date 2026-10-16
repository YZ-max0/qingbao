"""批量分析竞品，结果分别落盘，并同步回自建情报系统。

目标来源（按优先级）：
  1. 命令行参数      python run_batch_analysis.py 海柔创新 快仓智能
  2. 自建系统的 config/competitors.yaml —— 两套系统共用同一份竞品名单，
     避免在两边各维护一份

用法：
    python run_batch_analysis.py                      # 第一梯队全部
    python run_batch_analysis.py --tier tier2         # 第二梯队
    python run_batch_analysis.py 海柔创新 快仓智能     # 指定
"""
import argparse
import asyncio
import io
import os
import sys
import time

sys.path.insert(0, r'D:\Harness-spaces\competitor-intel')

from core.orchestrator import Orchestrator  # noqa: E402

OUTDIR = r'D:\Harness-spaces\competitor-reports'
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'batch_analysis.log')
OUR = '磅旗科技（仓储AMR：料箱机器人、货到人、无人叉车、仓储集成）'
# 与自建情报系统共用同一份竞品名单
COMPETITORS_YAML = r'D:\Harness-spaces\爬取\config\competitors.yaml'

# 报告生成后自动同步到自建看板（reports 表 + events）
sys.path.insert(0, r'D:\Harness-spaces\爬取')
try:
    from src.adapters import report_sync_adapter, context_exporter, source_import_adapter
except Exception:  # noqa: BLE001
    report_sync_adapter = None
    context_exporter = None
    source_import_adapter = None


def load_targets(tier: str | None, names: list[str]) -> list[str]:
    """按 命令行 > 指定梯队 > 第一梯队 的顺序取目标。"""
    if names:
        return names

    try:
        import yaml
        with io.open(COMPETITORS_YAML, encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        print(f'!! 读取 {COMPETITORS_YAML} 失败: {e}')
        return []

    tiers = cfg.get('tiers') or {}
    key = tier or 'tier1'
    items = tiers.get(key) or []
    return [c['name'] for c in items if isinstance(c, dict) and c.get('name')]


def log(m):
    with io.open(LOG, 'a', encoding='utf-8') as f:
        f.write(str(m) + '\n')


def parse_args():
    ap = argparse.ArgumentParser(description='批量竞品分析')
    ap.add_argument('names', nargs='*', help='竞品名称（留空则按梯队取）')
    ap.add_argument('--tier', default=None,
                    choices=['tier1', 'tier2', 'tier3_overseas', 'tier3_platform'],
                    help='从 competitors.yaml 取哪个梯队（默认 tier1）')
    ap.add_argument('--no-sync', action='store_true',
                    help='不同步到自建情报看板')
    return ap.parse_args()


async def main():
    args = parse_args()
    targets = load_targets(args.tier, args.names)
    if not targets:
        log('!! 没有可分析的目标（检查命令行参数或 competitors.yaml）')
        return 1

    os.makedirs(OUTDIR, exist_ok=True)
    orch = Orchestrator()

    log(f'=== 批量分析 {len(targets)} 个竞品 ===')
    log('目标: ' + ', '.join(targets))

    # 方向 1：把自建系统已采集的事实注入分析上下文（上游 prior_context）
    priors: dict[str, str] = {}
    if context_exporter is not None:
        try:
            priors = context_exporter.export_contexts(targets, days=180)
            log(f'已注入内部上下文: {len(priors)}/{len(targets)} 个竞品')
            for k, v in priors.items():
                log(f'   {k}: {len(v)} 字符')
        except Exception as e:
            log(f'!! 导出上下文失败（继续，不影响分析）: {e}')
    log('')

    t0 = time.time()
    try:
        results = await orch.run_batch(
            targets, our_product=OUR, prior_contexts=priors
        )
    except Exception as e:
        import traceback
        log(f'!! 批量调用失败: {type(e).__name__} {str(e)[:200]}')
        log(traceback.format_exc()[-1500:])
        return 1

    log(f'批量返回 {len(results)} 个结果，耗时 {time.time()-t0:.0f}s')

    ok = 0
    for r in results:
        name = r.get('competitor', 'unknown')
        report = (r.get('strategy') or {}).get('strategy') or r.get('report') or ''
        if not report:
            # 兜底：从各阶段拼一个
            parts = []
            for k in ('research', 'analysis', 'product', 'strategy', 'evaluation'):
                v = r.get(k)
                if isinstance(v, dict):
                    for kk in ('summary', 'analysis', 'strategy'):
                        if v.get(kk):
                            parts.append(f'## {k}\n\n{v[kk]}')
                            break
            report = '\n\n---\n\n'.join(parts)

        safe = name.replace('/', '_').replace(' ', '_')
        path = os.path.join(OUTDIR, f'{safe}.md')
        with io.open(path, 'w', encoding='utf-8') as f:
            f.write(f'# {name} 竞品分析报告\n\n')
            f.write(f'> 我方产品：{OUR}\n\n')
            f.write(report)
        log(f'  [OK] {name} -> {os.path.basename(path)} ({len(report)} 字符)')
        ok += 1

    log('')
    log(f'完成 {ok}/{len(targets)}，输出目录: {OUTDIR}')

    # 方向 2：把搜索到的来源回补进自建库（覆盖自建源够不到的竞品）
    # 注意：只看"新增条数"会误判 —— 对已有大量直采数据的竞品，
    # Tavily 搜到的 URL 多半已入库，去重后为 0 属正常。
    # 关键指标是 new_coverage：原本零覆盖的竞品是否被填补。
    if not args.no_sync and source_import_adapter is not None:
        total_new, total_dup, filled = 0, 0, []
        for r in results:
            name = r.get('competitor', '')
            if not name:
                continue
            try:
                st = source_import_adapter.import_from_result(name, r)
                total_new += st.get('inserted', 0)
                total_dup += st.get('skipped', 0)
                if st.get('new_coverage'):
                    filled.append(name)
            except Exception as e:
                log(f'!! 回补 {name} 的来源失败: {e}')
        log(f'来源回补: 新增 {total_new} 条 / 重复 {total_dup} 条')
        if filled:
            log(f'★ 填补空白竞品 ({len(filled)} 个): {", ".join(filled)}')
        else:
            log('  本次无空白竞品被填补（原覆盖已足够）')

    # 同步报告到自建看板（深度分析频道）
    if not args.no_sync and report_sync_adapter is not None:
        try:
            st = report_sync_adapter.run()
            log(f'已同步报告到自建看板: {st}')
        except Exception as e:
            log(f'!! 同步报告失败: {e}')
    elif not args.no_sync:
        log('!! 未能导入 report_sync_adapter，跳过同步')

    return 0


raise SystemExit(asyncio.run(main()))
