\getenv agent_password AGENT_DB_PASSWORD
create role newsletter_agent login password :'agent_password';
create database newsletter_agent owner newsletter_agent;
revoke all on database newsletter_agent from public;
revoke connect on database postgres from public;
revoke connect on database template1 from public;
