-- قائمة الاستمرارية: الزبون يختار بنفسه Start لبوت TOTP الاحتياطي.
-- شغّل هذا الملف مرة واحدة في Supabase SQL Editor قبل النشر.

create table if not exists public.continuity_bot_contacts (
  customer_chat_id bigint primary key,
  customer_name text,
  customer_username text,
  started_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

-- دعوة واحدة فقط لكل زبون بعد التسليم. لا نعيد إرسالها عند تجديد الحساب.
create table if not exists public.continuity_bot_invites (
  customer_chat_id bigint primary key,
  business_connection_id text not null,
  due_at timestamptz not null,
  status text not null default 'scheduled'
    check (status in ('scheduled', 'sent', 'started', 'failed', 'cancelled')),
  sent_at timestamptz,
  started_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists continuity_bot_invites_due_idx
  on public.continuity_bot_invites (due_at)
  where status = 'scheduled';

alter table public.continuity_bot_contacts enable row level security;
alter table public.continuity_bot_invites enable row level security;

revoke all on table public.continuity_bot_contacts from anon, authenticated;
revoke all on table public.continuity_bot_invites from anon, authenticated;

grant select, insert, update, delete on table public.continuity_bot_contacts to service_role;
grant select, insert, update, delete on table public.continuity_bot_invites to service_role;
