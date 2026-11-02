CREATE TABLE employees (
 max_id bigint PRIMARY KEY, full_name text NOT NULL DEFAULT '',
 role text NOT NULL DEFAULT 'employee' CHECK(role IN ('employee','admin')),
 status text NOT NULL DEFAULT 'new' CHECK(status IN ('new','pending','approved','blocked')),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE shifts (
 id bigserial PRIMARY KEY, employee_id bigint NOT NULL REFERENCES employees(max_id),
 started_at timestamptz NOT NULL, ended_at timestamptz,
 version integer NOT NULL DEFAULT 1,
 CHECK(ended_at IS NULL OR ended_at > started_at)
);
CREATE UNIQUE INDEX one_open_shift ON shifts(employee_id) WHERE ended_at IS NULL;
CREATE INDEX shifts_employee_time ON shifts(employee_id,started_at);
CREATE TABLE breaks (
 id bigserial PRIMARY KEY, shift_id bigint NOT NULL REFERENCES shifts(id),
 started_at timestamptz NOT NULL, ended_at timestamptz,
 CHECK(ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX one_open_break ON breaks(shift_id) WHERE ended_at IS NULL;
CREATE TABLE corrections (
 id bigserial PRIMARY KEY, shift_id bigint NOT NULL REFERENCES shifts(id),
 author_id bigint NOT NULL REFERENCES employees(max_id),
 created_at timestamptz NOT NULL DEFAULT now(), reason text NOT NULL,
 old_values jsonb NOT NULL, new_values jsonb NOT NULL, base_version integer NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 reviewer_id bigint REFERENCES employees(max_id), reviewed_at timestamptz
);
CREATE TABLE audit (
 id bigserial PRIMARY KEY, correction_id bigint NOT NULL REFERENCES corrections(id),
 author_id bigint NOT NULL REFERENCES employees(max_id), reviewer_id bigint NOT NULL REFERENCES employees(max_id),
 changed_at timestamptz NOT NULL DEFAULT now(), reason text NOT NULL,
 old_values jsonb NOT NULL, new_values jsonb NOT NULL
);
CREATE TABLE inbox (
 id bigserial PRIMARY KEY, event_key text NOT NULL UNIQUE, payload jsonb NOT NULL,
 received_at timestamptz NOT NULL DEFAULT now(), processed_at timestamptz,
 attempts integer NOT NULL DEFAULT 0, next_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE outbox (
 id bigserial PRIMARY KEY, recipient bigint NOT NULL REFERENCES employees(max_id),
 body jsonb NOT NULL, csv_data bytea, sensitive boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz,
 attempts integer NOT NULL DEFAULT 0, next_at timestamptz NOT NULL DEFAULT now(),
 failed boolean NOT NULL DEFAULT false
);
CREATE INDEX inbox_pending ON inbox(id) WHERE processed_at IS NULL;
CREATE INDEX outbox_pending ON outbox(id) WHERE sent_at IS NULL AND NOT failed;
