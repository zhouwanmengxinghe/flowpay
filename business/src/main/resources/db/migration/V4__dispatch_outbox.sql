CREATE TABLE outbox_events (
 id varchar(64) PRIMARY KEY,
 event_type varchar(40) NOT NULL,
 run_id varchar(64) NOT NULL REFERENCES runs,
 schema_version integer NOT NULL DEFAULT 1,
 payload jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now(),
 published_at timestamptz,
 attempt integer NOT NULL DEFAULT 0,
 available_at timestamptz NOT NULL DEFAULT now(),
 publisher_owner varchar(160),
 publisher_lease_until timestamptz
);
CREATE INDEX outbox_ready ON outbox_events(available_at,created_at)
 WHERE published_at IS NULL;
