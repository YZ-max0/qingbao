-- 仓储 AMR 竞品情报系统 —— SQLite 表结构
-- 设计要点：API 通路与爬虫通路写入同一张 events 表，用 source_type 区分

PRAGMA journal_mode = WAL;

-- ── 统一事件表：所有情报的唯一入口 ──────────────────────────
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_hash    TEXT    NOT NULL UNIQUE,   -- 去重指纹
    source_type   TEXT    NOT NULL,          -- api | rss | scrape
    source_name   TEXT    NOT NULL,          -- 如 "中国政府采购网" / "工信部政策"
    channel       TEXT,                      -- policy | tender | announcement | site_change | media
    entity        TEXT,                      -- 关联主体，如 "海柔创新"
    entity_tier   TEXT,                      -- tier1 | tier2 | tier3 | unknown
    title         TEXT    NOT NULL,
    summary       TEXT,
    url           TEXT,
    published_at  TEXT,                      -- 原始发布时间 ISO8601
    captured_at   TEXT    NOT NULL,          -- 抓取时间 ISO8601
    relevance     REAL    DEFAULT 0,         -- 相关度打分 0~1
    tags          TEXT,                      -- JSON 数组
    raw_path      TEXT,                      -- 原始快照文件路径（可溯源）
    status        TEXT    DEFAULT 'new'      -- new | read | archived
);

CREATE INDEX IF NOT EXISTS idx_events_channel     ON events(channel);
CREATE INDEX IF NOT EXISTS idx_events_entity      ON events(entity);
CREATE INDEX IF NOT EXISTS idx_events_published   ON events(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_source_type ON events(source_type);

-- ── 竞品表：从 competitors.yaml 同步 ────────────────────────
CREATE TABLE IF NOT EXISTS competitors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    en_name     TEXT,
    tier        TEXT,                        -- tier1 | tier2 | tier3_overseas | tier3_platform
    focus       TEXT,
    list_status TEXT,
    stock_code  TEXT,
    site_url    TEXT,
    updated_at  TEXT
);

-- ── 监控目标表：官网变更监控点 ──────────────────────────────
CREATE TABLE IF NOT EXISTS watch_targets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor    TEXT NOT NULL,
    kind          TEXT NOT NULL,             -- product | pricing | news | careers
    url           TEXT NOT NULL UNIQUE,
    last_hash     TEXT,                      -- 上次内容指纹
    last_checked  TEXT,
    enabled       INTEGER DEFAULT 1
);

-- ── 运行日志：每次采集记录一行，便于排查 ────────────────────
CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    fetched     INTEGER DEFAULT 0,
    inserted    INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    errors      TEXT
);
