-- 告警去重：记录已推送过的事件，避免同一信号反复打扰
CREATE TABLE IF NOT EXISTS alerts_sent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL,
    channel     TEXT NOT NULL,          -- email | dingtalk | webhook
    sent_at     TEXT NOT NULL,
    ok          INTEGER DEFAULT 1,
    error       TEXT,
    UNIQUE(event_id, channel)
);
