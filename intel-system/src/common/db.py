"""统一事件入库：API 通路与爬虫通路都写这张表。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .util import DB_PATH, ROOT, ensure_dirs


@contextmanager
def connect(db_path: Path = DB_PATH):
    ensure_dirs()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    for name in ("schema.sql", "schema_alert.sql", "schema_reports.sql"):
        f = ROOT / "db" / name
        if not f.exists():
            continue
        with connect(db_path) as conn:
            conn.executescript(f.read_text(encoding="utf-8"))
    ensure_ui_columns(db_path)


def ensure_ui_columns(db_path: Path = DB_PATH) -> None:
    """补上看板需要的列。

    SQLite 的 ADD COLUMN 不支持 IF NOT EXISTS，重复执行会抛
    "duplicate column name"，这里逐条执行并忽略该错误，保证幂等。
    """
    for name in ("schema_ui.sql", "schema_llm.sql", "schema_report_brief.sql"):
        f = ROOT / "db" / name
        if not f.exists():
            continue
        # 逐行剥掉注释再去分号切分：直接按 ";" 切会把首个语句和文件头注释粘在一起，
        # 导致以 "--" 开头的判断误杀第一条 ALTER。
        lines = [
            ln for ln in f.read_text(encoding="utf-8").splitlines()
            if not ln.strip().startswith("--")
        ]
        stmts = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
        with connect(db_path) as conn:
            for stmt in stmts:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def event_hash(source_name: str, title: str, url: str | None) -> str:
    """去重指纹：同源同标题同URL 视为同一事件。"""
    key = f"{source_name}|{title.strip()}|{(url or '').strip()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def save_raw(source_name: str, content: str, suffix: str = ".html") -> str:
    """保存原始快照，保证可溯源。"""
    ensure_dirs()
    from .util import RAW_DIR

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in source_name)[:40]
    path = RAW_DIR / f"{stamp}_{safe}{suffix}"
    path.write_text(content, encoding="utf-8", errors="replace")
    return str(path.relative_to(ROOT))


def insert_event(conn: sqlite3.Connection, **kw) -> bool:
    """插入一条事件；重复返回 False。"""
    title = (kw.get("title") or "").strip()
    if not title:
        return False

    source_name = kw.get("source_name", "unknown")
    url = kw.get("url")
    row = {
        "event_hash": event_hash(source_name, title, url),
        "source_type": kw.get("source_type", "scrape"),
        "source_name": source_name,
        "channel": kw.get("channel"),
        "entity": kw.get("entity"),
        "entity_tier": kw.get("entity_tier"),
        "title": title,
        "summary": kw.get("summary"),
        "url": url,
        "published_at": kw.get("published_at"),
        "captured_at": kw.get("captured_at") or now_iso(),
        "relevance": float(kw.get("relevance") or 0),
        "tags": json.dumps(kw.get("tags") or [], ensure_ascii=False),
        "raw_path": kw.get("raw_path"),
        "status": "new",
    }
    cols = ", ".join(row)
    marks = ", ".join(f":{c}" for c in row)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO events ({cols}) VALUES ({marks})", row
    )
    return cur.rowcount > 0


def start_run(conn: sqlite3.Connection, job: str) -> int:
    cur = conn.execute(
        "INSERT INTO run_log (job, started_at) VALUES (?, ?)", (job, now_iso())
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    fetched: int,
    inserted: int,
    skipped: int,
    errors: str = "",
) -> None:
    conn.execute(
        "UPDATE run_log SET finished_at=?, fetched=?, inserted=?, skipped=?, errors=? "
        "WHERE id=?",
        (now_iso(), fetched, inserted, skipped, errors, run_id),
    )
