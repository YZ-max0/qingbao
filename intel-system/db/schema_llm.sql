-- 正文与 LLM 提炼结果
-- body        : 抓取到的正文纯文本（'' 表示抓过但无正文，NULL 表示尚未尝试）
-- body_tries  : 尝试次数，>=3 时不再重试
ALTER TABLE events ADD COLUMN body TEXT;
ALTER TABLE events ADD COLUMN body_tries INTEGER DEFAULT 0;
ALTER TABLE events ADD COLUMN llm_digest TEXT;
ALTER TABLE events ADD COLUMN llm_impact TEXT;
ALTER TABLE events ADD COLUMN llm_summary TEXT;
