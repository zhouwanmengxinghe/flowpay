ALTER TABLE runs ADD COLUMN attempt integer NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN lease_until timestamptz;
ALTER TABLE runs ADD COLUMN available_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE runs ADD COLUMN started_at timestamptz;
ALTER TABLE runs ADD COLUMN cancel_sent boolean NOT NULL DEFAULT false;
ALTER TABLE runs ADD COLUMN execution_mode varchar(16) NOT NULL DEFAULT 'legacy';
ALTER TABLE runs ADD COLUMN execution_engine varchar(16) NOT NULL DEFAULT 'legacy';
ALTER TABLE runs ADD COLUMN metrics jsonb NOT NULL DEFAULT '{}';
CREATE INDEX runs_dispatch ON runs(available_at,created_at) WHERE status='queued';
CREATE TABLE user_permissions (
 user_id varchar(64) NOT NULL REFERENCES app_users, permission varchar(32) NOT NULL
 CHECK(permission IN ('MERCHANT_ADMIN','SUPPORT_ADMIN','OBSERVE')), PRIMARY KEY(user_id,permission)
);
CREATE TABLE business_audit (
 id bigserial PRIMARY KEY, actor_id varchar(64) NOT NULL REFERENCES app_users,
 tenant_id varchar(64) NOT NULL REFERENCES tenants, action varchar(64) NOT NULL,
 target_id varchar(128) NOT NULL, payload jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX business_audit_tenant ON business_audit(tenant_id,id DESC);
CREATE TABLE tenant_limits (
 tenant_id varchar(64) PRIMARY KEY REFERENCES tenants, requests_per_minute integer NOT NULL DEFAULT 30 CHECK(requests_per_minute>0),
 daily_runs integer NOT NULL DEFAULT 100 CHECK(daily_runs>0), active_runs integer NOT NULL DEFAULT 2 CHECK(active_runs>0)
);
INSERT INTO tenant_limits(tenant_id) SELECT id FROM tenants;
