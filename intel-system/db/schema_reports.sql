-- competitor-intel 深度分析报告回写
-- 与 events 分开存：报告很长（数千字），塞进 events 会拖慢列表查询。
-- events 里只留一条摘要级记录（channel='analysis'），详情查这张表。

CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor    TEXT NOT NULL,
    our_product   TEXT,
    report        TEXT NOT NULL,         -- 完整 markdown 报告
    report_hash   TEXT NOT NULL,         -- 内容哈希，用于去重与判断是否更新
    strategy      TEXT,                  -- 战略章节（SWOT/威胁/行动）
    score         REAL,                  -- 质量分
    tokens        INTEGER,               -- 消耗 token 数
    cost_usd      REAL,                  -- 估算成本
    elapsed_sec   REAL,                  -- 耗时
    generated_at  TEXT NOT NULL,
    UNIQUE(competitor, report_hash)
);

CREATE INDEX IF NOT EXISTS idx_reports_competitor ON reports(competitor);
CREATE INDEX IF NOT EXISTS idx_reports_generated  ON reports(generated_at DESC);
