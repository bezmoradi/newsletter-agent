create table sources (
    id bigint generated always as identity primary key,
    newsletter text not null,
    key text not null,
    type text not null,
    url text,
    etag text,
    last_modified text,
    first_seen_at timestamptz,
    last_checked_at timestamptz,
    last_success_at timestamptz,
    consecutive_failures integer not null default 0,
    last_error text,
    last_new_item_at timestamptz,
    unique (newsletter, key)
);

create table items (
    id bigint generated always as identity primary key,
    newsletter text not null,
    source_id bigint not null references sources (id),
    external_id text not null,
    url text not null,
    canonical_url text not null,
    title text,
    published_at timestamptz,
    summary text,
    state text not null check (
        state in ('baseline', 'new', 'enriched', 'enrich_failed', 'assessed', 'assess_failed')
    ),
    assess_attempts integer not null default 0,
    first_seen_at timestamptz not null default now(),
    unique (source_id, external_id),
    unique (newsletter, canonical_url)
);

create index items_newsletter_state_idx on items (newsletter, state);

create table item_content (
    item_id bigint primary key references items (id),
    kind text not null check (kind in ('article', 'transcript')),
    text text,
    fetched_at timestamptz not null default now(),
    error text
);

create table runs (
    id bigint generated always as identity primary key,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    status text not null check (
        status in ('running', 'succeeded', 'partial', 'failed', 'abandoned')
    ),
    counts_json jsonb not null default '{}',
    usage_json jsonb not null default '{}',
    error text,
    claude_unavailable boolean not null default false,
    notice_sent boolean not null default false
);

create table assessments (
    id bigint generated always as identity primary key,
    item_id bigint not null references items (id),
    model text not null,
    prompt_version text not null,
    profile_hash text not null,
    score smallint not null check (score between 0 and 10),
    reason text not null,
    run_id bigint references runs (id),
    created_at timestamptz not null default now(),
    unique (item_id, prompt_version, model)
);

create table digests (
    id bigint generated always as identity primary key,
    run_id bigint not null references runs (id),
    newsletter text not null,
    body_json jsonb not null,
    model text,
    prompt_version text not null,
    fallback boolean not null default false,
    created_at timestamptz not null default now(),
    sent_at timestamptz,
    send_error text,
    unique (run_id, newsletter)
);

create table digest_items (
    digest_id bigint not null references digests (id),
    item_id bigint not null unique references items (id),
    position integer not null,
    included boolean not null
);
