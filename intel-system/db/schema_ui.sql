-- 看板所需的增量字段（幂等，可重复执行）
-- 说明：SQLite 不支持 IF NOT EXISTS 加列，重复执行会报错，
-- 由 Python 侧捕获 "duplicate column name" 忽略。

ALTER TABLE events ADD COLUMN note TEXT;
ALTER TABLE events ADD COLUMN starred INTEGER DEFAULT 0;
ALTER TABLE events ADD COLUMN updated_at TEXT;
