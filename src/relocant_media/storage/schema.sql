-- Initial data-model blueprint. Production migrations will be created with Alembic.

create table if not exists source (
    id text primary key,
    city text not null,
    name text not null,
    url text not null,
    source_type text not null,
    trust numeric(4,3) not null,
    enabled boolean not null default true
);

create table if not exists raw_item (
    id bigserial primary key,
    source_id text not null references source(id),
    external_id text not null,
    url text not null,
    title text not null,
    body text not null,
    published_at timestamptz,
    ingested_at timestamptz not null default now(),
    unique(source_id, external_id)
);

create table if not exists post (
    id bigserial primary key,
    city text not null,
    category text not null,
    status text not null,
    title text not null,
    body text not null,
    confidence numeric(5,4) not null,
    telegram_message_id bigint,
    published_at timestamptz,
    created_at timestamptz not null default now()
);

create table if not exists post_snapshot (
    post_id bigint not null references post(id),
    captured_at timestamptz not null,
    views bigint not null default 0,
    reactions bigint not null default 0,
    comments bigint not null default 0,
    forwards bigint not null default 0,
    subscriber_delta bigint,
    primary key(post_id, captured_at)
);

create table if not exists submission (
    id bigserial primary key,
    kind text not null,
    telegram_user_id bigint,
    telegram_message_id bigint,
    payload jsonb not null,
    status text not null default 'new',
    created_at timestamptz not null default now()
);

create table if not exists moderation_action (
    id bigserial primary key,
    telegram_chat_id bigint not null,
    telegram_message_id bigint not null,
    action text not null,
    reason text,
    model text,
    created_at timestamptz not null default now()
);

create table if not exists ad_request (
    id bigserial primary key,
    telegram_user_id bigint,
    payload jsonb not null,
    status text not null default 'new',
    quoted_price numeric(12,2),
    currency text,
    created_at timestamptz not null default now()
);

create table if not exists daily_report (
    report_date date primary key,
    city text not null,
    payload jsonb not null,
    created_at timestamptz not null default now()
);
