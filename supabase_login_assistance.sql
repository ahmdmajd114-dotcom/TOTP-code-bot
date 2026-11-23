-- Independent sign-in guidance; no passwords, TOTP seeds, money or durations.
begin;
create table if not exists public.chatgpt_login_sessions (
 id uuid primary key default gen_random_uuid(),
 owner_id bigint not null, chat_id bigint not null, account_id text not null,
 business_id text not null,
 stage text not null check(stage in ('delivered','email','password','passkey','clarify','authenticator','review','complete')),
 delivery_message_id bigint not null default 0,
 last_message_id bigint not null default 0,
 due_at timestamptz,
 note_status text not null default 'skipped' check(note_status in ('queued','sending','sent','skipped')),
 updated_at timestamptz not null default now(),
 unique(owner_id,chat_id,account_id)
);
create index if not exists chatgpt_login_due_idx on public.chatgpt_login_sessions(due_at) where note_status='queued';
alter table public.chatgpt_login_sessions enable row level security;
revoke all on public.chatgpt_login_sessions from public, anon, authenticated;
grant select,insert,update,delete on public.chatgpt_login_sessions to service_role;

create or replace function public.start_chatgpt_login(p_owner bigint,p_chat bigint,p_account text,p_business text,p_message bigint,p_sent timestamptz default now())
returns boolean language sql set search_path=public as $$
 with changed as (
 insert into chatgpt_login_sessions(owner_id,chat_id,account_id,business_id,stage,delivery_message_id,last_message_id,due_at,note_status)
 values(p_owner,p_chat,p_account,p_business,'delivered',p_message,p_message,p_sent+interval '5 seconds','queued')
 on conflict(owner_id,chat_id,account_id) do update set business_id=excluded.business_id,
 stage='delivered',delivery_message_id=p_message,last_message_id=p_message,
 due_at=excluded.due_at,note_status='queued',updated_at=now()
 where chatgpt_login_sessions.last_message_id < p_message returning id
 ) select exists(select 1 from changed);
$$;
revoke all on function public.start_chatgpt_login(bigint,bigint,text,text,bigint,timestamptz) from public,anon,authenticated;
grant execute on function public.start_chatgpt_login(bigint,bigint,text,text,bigint,timestamptz) to service_role;
commit;
