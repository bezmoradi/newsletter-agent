alter table items add column title_score smallint;

alter table assessments add column score_without_ratings smallint;

create table profile_suggestions (
    id bigint generated always as identity primary key,
    newsletter text not null,
    run_id bigint not null references runs (id),
    body_json jsonb not null,
    created_at timestamptz not null default now()
);
