-- 已发布迁移不可改写：为取消确认增加有界重试状态。
ALTER TABLE run_attempts ADD COLUMN cancel_attempt integer NOT NULL DEFAULT 0;
ALTER TABLE run_attempts ADD COLUMN cancel_available_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE run_attempts ADD COLUMN cancel_deadline_at timestamptz;

DROP INDEX run_attempts_cancel;
CREATE INDEX run_attempts_cancel ON run_attempts(cancel_available_at)
 WHERE state='canceled' AND cancel_ack_at IS NULL;
