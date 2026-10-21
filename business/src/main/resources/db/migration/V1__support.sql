CREATE TABLE tenants (
 id varchar(64) PRIMARY KEY, name varchar(120) NOT NULL
);
CREATE TABLE app_users (
 id varchar(64) PRIMARY KEY, username varchar(80) UNIQUE NOT NULL,
 password_hash text NOT NULL, tenant_id varchar(64) NOT NULL REFERENCES tenants,
 role varchar(16) NOT NULL CHECK(role IN ('MERCHANT','SUPPORT')), active boolean NOT NULL DEFAULT true
);
CREATE TABLE tenant_accounts (
 tenant_id varchar(64) NOT NULL REFERENCES tenants, account_id varchar(100) NOT NULL,
 PRIMARY KEY(tenant_id,account_id), UNIQUE(account_id)
);
-- 演示中技术支持也按商户授权，不使用“支持人员可读所有商户”的隐式规则。
CREATE TABLE support_grants (
 user_id varchar(64) NOT NULL REFERENCES app_users, tenant_id varchar(64) NOT NULL REFERENCES tenants,
 PRIMARY KEY(user_id,tenant_id)
);
CREATE TABLE auth_tokens (
 token_hash char(64) PRIMARY KEY, user_id varchar(64) NOT NULL REFERENCES app_users,
 expires_at timestamptz NOT NULL
);
CREATE INDEX auth_tokens_expiry ON auth_tokens(expires_at);
CREATE TABLE sessions (
 id varchar(64) PRIMARY KEY, tenant_id varchar(64) NOT NULL REFERENCES tenants,
 owner_id varchar(64) NOT NULL REFERENCES app_users,
 account_id varchar(100) NOT NULL, environment varchar(16) NOT NULL CHECK(environment IN ('production','sandbox')),
 title varchar(120) NOT NULL DEFAULT '新的调查', status varchar(20) NOT NULL DEFAULT 'idle',
 checkpoint jsonb NOT NULL DEFAULT '{}', next_seq bigint NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(tenant_id,account_id) REFERENCES tenant_accounts(tenant_id,account_id)
);
CREATE INDEX sessions_tenant_created ON sessions(tenant_id,created_at DESC);
CREATE TABLE runs (
 id varchar(64) PRIMARY KEY, session_id varchar(64) NOT NULL REFERENCES sessions,
 request_id varchar(128) NOT NULL, input text NOT NULL, status varchar(20) NOT NULL,
 answer text, evidence jsonb NOT NULL DEFAULT '[]', error text, created_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz,
 UNIQUE(session_id,request_id)
);
CREATE UNIQUE INDEX runs_one_active ON runs(session_id) WHERE status IN ('queued','running');
CREATE TABLE session_events (
 session_id varchar(64) NOT NULL REFERENCES sessions, seq bigint NOT NULL,
 run_id varchar(64) REFERENCES runs, type varchar(80) NOT NULL, payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(session_id,seq)
);
CREATE TABLE tickets (
 id varchar(64) PRIMARY KEY, tenant_id varchar(64) NOT NULL REFERENCES tenants,
 session_id varchar(64) NOT NULL REFERENCES sessions, created_by varchar(64) NOT NULL REFERENCES app_users,
 request_id varchar(128) NOT NULL, title varchar(160) NOT NULL,
 evidence jsonb NOT NULL, status varchar(20) NOT NULL DEFAULT 'OPEN'
   CHECK(status IN ('OPEN','ASSIGNED','IN_PROGRESS','RESOLVED','CLOSED')),
 assignee_id varchar(64) REFERENCES app_users, version integer NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(tenant_id,request_id)
);
CREATE INDEX tickets_tenant_created ON tickets(tenant_id,created_at DESC);
CREATE TABLE ticket_history (
 id bigserial PRIMARY KEY, ticket_id varchar(64) NOT NULL REFERENCES tickets,
 actor_id varchar(64) NOT NULL REFERENCES app_users, from_status varchar(20), to_status varchar(20) NOT NULL,
 note varchar(2000) NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ticket_history_ticket ON ticket_history(ticket_id,id);
