"""告警端到端测试：起本地 HTTP 服务器，验证推送与去重，不影响真实渠道。

用法: python scripts/test_alert_e2e.py
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common import db
from src.report import alert

RECEIVED: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            RECEIVED.append(json.loads(body))
        except json.JSONDecodeError:
            RECEIVED.append({"raw": body})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):  # 静默
        pass


def main() -> int:
    import os
    import tempfile

    from src.common.util import CONFIG_DIR, load_yaml

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[test] mock webhook 启动于 127.0.0.1:{port}")

    # 临时把 webhook 渠道指向本地，其余渠道关闭
    cfg = load_yaml(CONFIG_DIR / "alerts.yaml")
    tmp = Path(tempfile.mkdtemp()) / "alerts.yaml"
    cfg.setdefault("channels", {})
    for name in ("email", "dingtalk"):
        cfg["channels"].setdefault(name, {})["enabled"] = False
    cfg["channels"].setdefault("webhook", {})
    cfg["channels"]["webhook"]["enabled"] = True
    cfg["channels"]["webhook"]["url_env"] = "TEST_ALERT_URL"

    import yaml

    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    # 重定向配置目录
    import src.report.alert as A

    A.CONFIG_DIR = tmp.parent
    (tmp.parent / "alerts.yaml").write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")

    os.environ["TEST_ALERT_URL"] = f"http://127.0.0.1:{port}/hook"

    with db.connect() as conn:
        conn.executescript(
            (db.ROOT / "db" / "schema_alert.sql").read_text(encoding="utf-8")
        )
        conn.execute("DELETE FROM alerts_sent WHERE channel='webhook'")

    # 断言"去重"必须看：第 2 次是否重复推送了第 1 次已推的同一批事件。
    # 单纯比较"第 2 次是否发送"是错的 —— 当符合条件的待推事件多于
    # max_items 时，第 2 次会把剩余的另一批事件推出去，那是正常的积压清理。
    print("\n[test] 第 1 次推送")
    A.run()
    first_titles = _titles(RECEIVED[-1]) if RECEIVED else []
    print(f"  收到 {len(first_titles)} 条")

    print("\n[test] 第 2 次推送（不得重复推送第 1 次的同一条事件）")
    before = len(RECEIVED)
    A.run()
    new_batches = RECEIVED[before:]
    second_titles = _titles(new_batches[-1]) if new_batches else []
    overlap = set(first_titles) & set(second_titles)

    server.shutdown()

    ok = bool(first_titles) and not overlap
    print(f"\n[test] 首批 {len(first_titles)} 条 / 第二批 {len(second_titles)} 条 / 重叠 {len(overlap)} 条")
    print("[test]", "通过 ✅ 已推送事件不会重复" if ok else f"失败 ❌ 重复推送: {list(overlap)[:3]}")
    return 0 if ok else 1


def _titles(payload: dict) -> list[str]:
    return [e.get("title", "") for e in (payload.get("events") or [])]


if __name__ == "__main__":
    raise SystemExit(main())
