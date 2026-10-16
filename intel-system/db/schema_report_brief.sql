-- 深度报告的 LLM 提炼结果（3~5 句要点，供看板右侧栏展示）
-- 原 strategy 字段是从报告中截取的段落（3000+ 字），不适合列表阅读，
-- 故新增独立字段存放真正压缩后的要点。
ALTER TABLE reports ADD COLUMN brief TEXT;
ALTER TABLE reports ADD COLUMN brief_at TEXT;
