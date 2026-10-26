-- Transactional state. Analytics (the matching tables) stay in DuckDB.
create table users (
    id bigserial primary key,
    username text not null unique check (username ~ '^[a-z0-9._-]{3,40}$'),
    display_name text not null default '',
    role text not null check (role in ('viewer', 'reviewer', 'approver', 'admin')),
    password_hash text not null,
    disabled boolean not null default false,
    failed_logins int not null default 0,
    locked_until timestamptz,
    created_at timestamptz not null default now(),
    last_login_at timestamptz
);

create table sessions (
    id bigserial primary key,
    token_hash text not null unique,                 -- sha256 of the cookie value; the raw token is never stored
    user_id bigint not null references users(id) on delete cascade,
    csrf_token text not null,
    created_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    expires_at timestamptz not null,
    ip text
);
create index sessions_user_idx on sessions(user_id);

-- A decision is a PROPOSAL by a maker until a different user (the checker) approves it.
create table decisions (
    id bigserial primary key,
    kind text not null,
    item_key text not null,
    action text not null check (action in ('confirm', 'reject', 'link', 'undo')),
    note text not null default '',
    payload jsonb,
    status text not null default 'proposed' check (status in ('proposed', 'approved', 'declined', 'withdrawn')),
    proposed_by bigint not null references users(id),
    proposed_at timestamptz not null default now(),
    decided_by bigint references users(id),
    decided_at timestamptz,
    decision_note text not null default '',
    auto_approved boolean not null default false,    -- only when four-eyes is switched off by configuration
    -- Defence in depth: even a bug in the application cannot let a user approve their own proposal.
    constraint four_eyes check (status <> 'approved' or auto_approved or decided_by <> proposed_by)
);
create unique index one_open_proposal on decisions(kind, item_key) where status = 'proposed';
create index decisions_item_idx on decisions(kind, item_key, status);

-- Append-only, hash-chained audit trail.
create table audit_log (
    id bigserial primary key,
    ts timestamptz not null default now(),
    actor text not null,
    actor_id bigint,
    action text not null,
    detail jsonb not null default '{}'::jsonb,
    ip text,
    prev_hash text not null,
    hash text not null
);
create function audit_immutable() returns trigger language plpgsql as $$
begin raise exception 'audit_log is append-only'; end $$;
create trigger audit_no_change before update or delete on audit_log for each row execute function audit_immutable();
create trigger audit_no_truncate before truncate on audit_log for each statement execute function audit_immutable();
