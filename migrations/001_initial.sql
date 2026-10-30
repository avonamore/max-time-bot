CREATE TABLE employees (
    max_id BIGINT PRIMARY KEY CHECK (max_id > 0),
    full_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'employee' CHECK (role IN ('employee','admin')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','blocked')),
    menu_token TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE shifts (
    id BIGSERIAL PRIMARY KEY,
    employee_id BIGINT NOT NULL REFERENCES employees(max_id),
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX one_open_shift ON shifts(employee_id) WHERE ended_at IS NULL;
CREATE INDEX shifts_employee_period ON shifts(employee_id,started_at,ended_at);
CREATE TABLE breaks (
    id BIGSERIAL PRIMARY KEY,
    shift_id BIGINT NOT NULL REFERENCES shifts(id),
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX one_open_break ON breaks(shift_id) WHERE ended_at IS NULL;
CREATE TABLE corrections (
    id BIGSERIAL PRIMARY KEY,
    employee_id BIGINT NOT NULL REFERENCES employees(max_id),
    shift_id BIGINT NOT NULL REFERENCES shifts(id),
    reason TEXT NOT NULL,
    old_values JSONB NOT NULL,
    new_values JSONB NOT NULL,
    shift_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by BIGINT REFERENCES employees(max_id),
    decided_at TIMESTAMPTZ
);
CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    author_id BIGINT NOT NULL REFERENCES employees(max_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    action TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    reason TEXT NOT NULL,
    old_values JSONB NOT NULL,
    new_values JSONB NOT NULL
);
CREATE TABLE inbox (
    id BIGSERIAL PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    event JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);
CREATE INDEX inbox_pending ON inbox(id) WHERE processed_at IS NULL;
CREATE TABLE outbox (
    id BIGSERIAL PRIMARY KEY,
    recipient_id BIGINT NOT NULL REFERENCES employees(max_id),
    kind TEXT NOT NULL CHECK (kind IN ('message','answer','csv')),
    payload JSONB NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    last_error TEXT
);
CREATE INDEX outbox_pending ON outbox(id) WHERE sent_at IS NULL AND failed_at IS NULL;
