alter table items add column publisher text;

update items set publisher = regexp_replace(
    lower(substring(url from '^[A-Za-z]+://([^/:?#@]+)')), '^www\.', ''
)
where source_id in (select id from sources where type = 'web_search');
