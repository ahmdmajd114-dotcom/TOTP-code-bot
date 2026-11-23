-- Waiting replies only. No financial effects or subscription changes.
begin;
create table if not exists public.customer_work_hours (
 owner_id bigint not null, chat_id bigint not null, work_day date not null,
 business_id text not null, first_message_id bigint not null,
 first_at timestamptz not null, due_at timestamptz not null,
 status text not null check(status in ('waiting','answered','code','sending','sent','expired')),
 revision bigint not null default 1, updated_at timestamptz not null default now(),
 primary key(owner_id,chat_id,work_day)
);
create index if not exists customer_work_hours_due_idx on public.customer_work_hours(due_at) where status='waiting';
alter table public.customer_work_hours enable row level security;
revoke all on public.customer_work_hours from public,anon,authenticated;
grant select,insert,update,delete on public.customer_work_hours to service_role;

create or replace function public.work_hours_event(p_owner bigint,p_chat bigint,p_business text,p_message bigint,p_kind text,p_at timestamptz default now())
returns jsonb language plpgsql set search_path=public as $$
declare local_time timestamp:=p_at at time zone 'Asia/Baghdad'; r customer_work_hours; s text;
begin
 if p_kind not in ('customer','owner','code') then raise exception 'Invalid work hours event'; end if;
 if local_time::time < time '08:00' or local_time::time >= time '16:00' then return null; end if;
 s:=case p_kind when 'owner' then 'answered' when 'code' then 'code' else 'waiting' end;
 insert into customer_work_hours(owner_id,chat_id,work_day,business_id,first_message_id,first_at,due_at,status)
 values(p_owner,p_chat,local_time::date,p_business,p_message,p_at,p_at+interval '15 minutes',s)
 on conflict(owner_id,chat_id,work_day) do update set
 business_id=excluded.business_id,
 status=case when p_kind='owner' then 'answered' when p_kind='code' then 'code' else customer_work_hours.status end,
 revision=customer_work_hours.revision+1,updated_at=now()
 returning * into r;
 return to_jsonb(r);
end;
$$;
revoke all on function public.work_hours_event(bigint,bigint,text,bigint,text,timestamptz) from public,anon,authenticated;
grant execute on function public.work_hours_event(bigint,bigint,text,bigint,text,timestamptz) to service_role;
notify pgrst,'reload schema';
commit;
