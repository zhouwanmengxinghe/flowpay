ALTER TABLE runs ADD COLUMN claim_version bigint NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN owner_instance varchar(160);
ALTER TABLE runs ADD COLUMN current_execution_id varchar(64);
ALTER TABLE runs ADD COLUMN deadline_at timestamptz;

CREATE TABLE run_attempts (
 execution_id varchar(64) PRIMARY KEY,
 run_id varchar(64) NOT NULL REFERENCES runs,
 claim_version bigint NOT NULL,
 owner_instance varchar(160) NOT NULL,
 agent_target varchar(300) NOT NULL,
 state varchar(16) NOT NULL CHECK(state IN ('running','completed','failed','canceled','expired')),
 lease_until timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz,
 cancel_ack_at timestamptz,
 UNIQUE(run_id,claim_version)
);
CREATE INDEX run_attempts_cancel ON run_attempts(created_at)
 WHERE state='canceled' AND cancel_ack_at IS NULL;

CREATE TABLE execution_slots (
 slot_id smallint PRIMARY KEY CHECK(slot_id BETWEEN 1 AND 2),
 execution_id varchar(64) UNIQUE REFERENCES run_attempts,
 lease_until timestamptz
);
INSERT INTO execution_slots(slot_id) VALUES (1),(2);

CREATE TABLE scheduler_instances (
 instance_id varchar(160) PRIMARY KEY,
 scheduler_mode varchar(16) NOT NULL CHECK(scheduler_mode IN ('single','distributed')),
 started_at timestamptz NOT NULL DEFAULT now(),
 last_seen timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX scheduler_instances_seen ON scheduler_instances(last_seen);
CREATE INDEX runs_current_claim ON runs(current_execution_id) WHERE status='running';
