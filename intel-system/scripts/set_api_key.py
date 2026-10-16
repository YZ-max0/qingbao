"""安全地设置/轮换 LLM API key。

为什么需要这个脚本：
    key 以明文存在 config/llm.local.yaml 里。轮换时若手工编辑，容易
    改错缩进或漏掉引号，导致配置静默失效（表现为"LLM 步骤被跳过"）。
    本脚本做三件事：写入 → 回读校验 → 真实调用验证。

用法：
    python scripts/set_api_key.py                 # 交互式输入（不回显）
    python scripts/set_api_key.py --provider deepseek
    python scripts/set_api_key.py --model deepseek-chat
    python scripts/set_api_key.py --test-only     # 只验证当前 key，不改动
    python scripts/set_api_key.py --show          # 显示当前配置（key 打码）

安全：
    - 交互输入不回显，也不进 shell 历史
    - 写入前自动备份原文件（.bak）
    - 只打印打码后的 key
"""
from __future__ import annotations

import argparse
import getpass
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.util import CONFIG_DIR, load_yaml  # noqa: E402

CONF = CONFIG_DIR / "llm.local.yaml"

PROVIDERS = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "custom": ("", ""),
}

TEMPLATE = """# LLM 凭据（本地私有文件，请勿提交到版本库）
#
# 优先级：环境变量 OPENAI_API_KEY > 本文件
# 想停用 LLM：把 enabled 改为 false，或直接删除本文件
#
# 安全提醒：本文件以明文保存密钥。请确保：
#   1. 不要提交到 git / 不要分享
#   2. 怀疑泄露时，立即运行 scripts/set_api_key.py 轮换

enabled: true

# 生成时间：{stamp}
provider: {provider}
base_url: {base_url}
model: {model}
api_key: "{api_key}"

# 调用参数
batch_size: 5          # 每批处理条数（越大越省调用次数，但单次耗时更长）
max_body_chars: 3000   # 送入模型的正文字数上限，控制 token 成本
timeout: 180           # 单次请求超时（秒）
"""


def mask(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return "(空)"
    if len(k) <= 12:
        return k[:3] + "*" * max(len(k) - 3, 1)
    return f"{k[:6]}...{k[-4:]}（长度 {len(k)}）"


def show() -> int:
    if not CONF.exists():
        print(f"配置文件不存在: {CONF}")
        return 1
    cfg = load_yaml(CONF) or {}
    print(f"配置文件: {CONF}")
    print(f"  enabled : {cfg.get('enabled')}")
    print(f"  provider: {cfg.get('provider')}")
    print(f"  base_url: {cfg.get('base_url')}")
    print(f"  model   : {cfg.get('model')}")
    print(f"  api_key : {mask(cfg.get('api_key', ''))}")
    return 0


def write_conf(api_key: str, provider: str, base_url: str, model: str) -> None:
    if CONF.exists():
        bak = CONF.with_suffix(f".yaml.{datetime.now():%Y%m%d_%H%M%S}.bak")
        shutil.copy2(CONF, bak)
        print(f"已备份原配置 -> {bak.name}")
    CONF.write_text(
        TEMPLATE.format(
            stamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            provider=provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
        ),
        encoding="utf-8",
    )
    print(f"已写入 {CONF}")


def test_key() -> int:
    """真实调用一次，确认 key 可用（捕获 401/402 等典型错误）。"""
    print("\n正在校验（发起一次真实调用）...")
    try:
        from src.report import llm
    except Exception as exc:  # noqa: BLE001
        print(f"  导入失败: {exc}")
        return 1

    if not llm.enabled():
        print("  未配置密钥，跳过")
        return 1

    try:
        res = llm._call([{
            "title": "连通性测试",
            "channel": "media",
            "summary": "",
            "body": "这是一条用于验证 API 连通性的测试内容。",
        }])
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        print(f"  ❌ 调用失败: {msg[:200]}")
        if "401" in msg or "invalid_api_key" in msg.lower():
            print("     -> 密钥无效或已被删除")
        elif "402" in msg or "insufficient" in msg.lower():
            print("     -> 余额不足，请充值")
        elif "404" in msg:
            print("     -> base_url 或 model 名称不正确")
        return 1

    if res:
        sample = next(iter(res.values()))
        print("  ✅ 调用成功")
        print(f"     样例摘要: {sample.get('summary', '')[:70]}")
        return 0
    print("  ⚠️ 调用返回成功但内容为空（模型未按格式返回），仍可用")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="设置/轮换 LLM API key")
    ap.add_argument("--provider", choices=list(PROVIDERS), default="deepseek",
                    help="服务商（决定 base_url 与默认模型）")
    ap.add_argument("--model", default=None, help="覆盖默认模型名")
    ap.add_argument("--base-url", default=None, help="覆盖默认 base_url（自定义服务用）")
    ap.add_argument("--key", default=None,
                    help="直接传入 key（不推荐：会留在命令历史里）")
    ap.add_argument("--test-only", action="store_true", help="只验证当前 key，不修改")
    ap.add_argument("--show", action="store_true", help="显示当前配置（key 打码）")
    args = ap.parse_args()

    if args.show:
        return show()
    if args.test_only:
        return test_key()

    base_url, model = PROVIDERS[args.provider]
    base_url = args.base_url or base_url
    model = args.model or model

    if not base_url:
        base_url = input("base_url（如 https://api.example.com/v1）: ").strip()
    if not model:
        model = input("model 名称: ").strip()

    key = args.key
    if not key:
        # 用 getpass 避免回显，也不会进 shell 历史
        key = getpass.getpass(f"请输入 {args.provider} 的 API key（输入不回显）: ").strip()
    if not key:
        print("未输入 key，已取消")
        return 1

    if not key.startswith("sk-") and args.provider != "custom":
        print(f"提示：该服务商的 key 通常以 'sk-' 开头，你输入的是 '{key[:4]}...'，继续执行。")

    write_conf(key, args.provider, base_url, model)
    print(f"  provider: {args.provider}")
    print(f"  base_url: {base_url}")
    print(f"  model   : {model}")
    print(f"  api_key : {mask(key)}")

    return test_key()


if __name__ == "__main__":
    raise SystemExit(main())
