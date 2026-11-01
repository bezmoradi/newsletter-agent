alter table items add column search_terms text[];
alter table items add column queued_at timestamptz;

alter table assessments add column feedback_ids bigint[];

alter table digests add column threshold smallint;

alter table digest_items add column reason text;
