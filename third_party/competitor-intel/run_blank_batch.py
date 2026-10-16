"""批量补齐所有空白竞品（分片执行，避免单次任务过长）。

用法：
    python run_blank_batch.py            # 全部 32 家
    python run_blank_batch.py 0 10       # 第 0~10 家（分片）
"""
import asyncio
import io
import json
import os
import sys
import time

sys.path.insert(0, r'D:\Harness-spaces\competitor-intel')
sys.path.insert(0, r'D:\Harness-spaces\爬取')

from core.orchestrator import Orchestrator  # noqa: E402
from src.adapters import (  # noqa: E402
    context_exporter, report_sync_adapter, source_import_adapter,
)

OUTDIR = r'D:\Harness-spaces\competitor-reports'
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'blank_batch.log')
BLANK = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'blank_targets.json')
OUR = '磅旗科技（仓储AMR：料箱机器人、货到人、无人叉车、仓储集成）'

# competitor-intel 单次批量不宜过大（并发过高会触发限流）
CHUNK = 3
# 批次之间的休眠（秒）。实测：连续跑 5 批后 6~8 批全部返回空报告，
# 是 DeepSeek 侧的突发限流；空响应会被 llm_retry 重试但重试耗尽后
# 仍返回空内容。批间停歇可显著降低触发概率。
BATCH_COOLDOWN = 20


def log(m):
    with io.open(LOG, 'a', encoding='utf-8') as f:
        f.write(str(m) + '\n')
    print(m, flush=True)


async def main():
    targets = json.loads(io.open(BLANK, encoding='utf-8').read())
    if len(sys.argv) >= 3:
        lo, hi = int(sys.argv[1]), int(sys.argv[2])
        targets = targets[lo:hi]

    log(f'=== 补齐空白竞品: {len(targets)} 家 ===')
    log('名单: ' + '、'.join(targets))
    log('')

    orch = Orchestrator()
    total_new = total_dup = 0
    filled = []
    t0 = time.time()

    for i in range(0, len(targets), CHUNK):
        batch = targets[i:i + CHUNK]
        if i:
            log(f'   (批间冷却 {BATCH_COOLDOWN}s，规避限流)')
            await asyncio.sleep(BATCH_COOLDOWN)
        log(f'--- 第 {i // CHUNK + 1} 批: {"、".join(batch)} ---')

        # 方向 1：注入自建上下文（这些是空白竞品，多半为空，但保留逻辑）
        try:
            priors = context_exporter.export_contexts(batch, days=180)
        except Exception as e:
            log(f'  注入上下文失败（忽略）: {e}')
            priors = {}

        try:
            results = await orch.run_batch(batch, our_product=OUR,
                                           prior_contexts=priors)
        except Exception as e:
            log(f'  !! 该批分析失败: {type(e).__name__} {str(e)[:160]}')
            continue

        # 限流会导致整批返回空报告（实测：连续跑 5 批后出现）。
        # 对空结果做一次延迟重试，避免整批白跑。
        def _is_empty(r):
            return not ((r.get('strategy') or {}).get('strategy'))

        empty_names = [r.get('competitor') for r in results
                       if r.get('competitor') and _is_empty(r)]
        if empty_names:
            log(f'  !! {len(empty_names)} 个返回空报告（疑似限流）: '
                f'{"、".join(empty_names)}')
            log(f'     冷却 {BATCH_COOLDOWN * 2}s 后重试...')
            await asyncio.sleep(BATCH_COOLDOWN * 2)
            try:
                retry = await orch.run_batch(
                    empty_names, our_product=OUR,
                    prior_contexts={k: v for k, v in priors.items()
                                    if k in empty_names},
                )
                retry_ok = [r.get('competitor') for r in retry
                            if not _is_empty(r)]
                # 用成功的重试结果替换原先的空结果
                results = [r for r in results
                           if r.get('competitor') not in retry_ok] + \
                          [r for r in retry if not _is_empty(r)]
                log(f'     重试成功 {len(retry_ok)} 个'
                    + (f': {"、".join(retry_ok)}' if retry_ok else ''))
            except Exception as e:
                log(f'     重试失败: {type(e).__name__} {str(e)[:120]}')

        for r in results:
            name = r.get('competitor', '')
            if not name:
                continue
            report = (r.get('strategy') or {}).get('strategy') or ''
            if not report:
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
            with io.open(os.path.join(OUTDIR, f'{safe}.md'), 'w',
                         encoding='utf-8') as f:
                f.write(f'# {name} 竞品分析报告\n\n> 我方产品：{OUR}\n\n{report}')

            # 方向 2：回补来源
            try:
                st = source_import_adapter.import_from_result(name, r)
                total_new += st.get('inserted', 0)
                total_dup += st.get('skipped', 0)
                if st.get('new_coverage'):
                    filled.append(name)
                log(f'  [OK] {name}: 报告 {len(report)} 字，'
                    f'回补 新增{st.get("inserted",0)}/重复{st.get("skipped",0)}')
            except Exception as e:
                log(f'  [OK] {name}: 报告 {len(report)} 字，回补失败 {e}')

    # 同步报告到看板
    try:
        st = report_sync_adapter.run()
        log(f'报告同步: {st}')
    except Exception as e:
        log(f'!! 报告同步失败: {e}')

    log('')
    log(f'=== 完成，耗时 {time.time()-t0:.0f}s ===')
    log(f'来源回补: 新增 {total_new} / 重复 {total_dup}')
    log(f'★ 填补空白竞品 {len(filled)} 个: {"、".join(filled)}')
    return 0


raise SystemExit(asyncio.run(main()))
