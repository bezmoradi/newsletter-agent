create table feedback (
    id bigint generated always as identity primary key,
    message_id text not null unique,
    item_id bigint not null references items (id),
    liked boolean not null,
    sent_at timestamptz not null,
    read_at timestamptz not null default now()
);

create index feedback_item_idx on feedback (item_id);
