create table instance (
    id boolean primary key default true check (id),
    feedback_token text not null default left(md5(gen_random_uuid()::text), 12)
);

insert into instance default values;
