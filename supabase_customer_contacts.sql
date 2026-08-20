-- سجل دائم للمستخدمين الذين تعاملوا مع المتجر.
-- شغّل هذا الملف مرة واحدة في Supabase SQL Editor.
create table if not exists public.customer_contacts (
  id bigint generated always as identity primary key,
  platform text not null check (platform in ('telegram', 'instagram')),
  external_id text not null,
  chat_id bigint,
  display_name text,
  username text,
  business_connection_id text,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique (platform, external_id)
);

create index if not exists customer_contacts_platform_idx
  on public.customer_contacts (platform, last_seen_at desc);

create index if not exists customer_contacts_chat_id_idx
  on public.customer_contacts (chat_id) where chat_id is not null;

alter table public.customer_contacts enable row level security;
revoke all on table public.customer_contacts from anon, authenticated;
grant select, insert, update, delete on table public.customer_contacts to service_role;
grant usage, select on sequence public.customer_contacts_id_seq to service_role;

-- ترحيل كل مستخدمي Telegram الموجودين في الأرشيف القديم.
insert into public.customer_contacts (
  platform, external_id, chat_id, display_name, username,
  first_seen_at, last_seen_at
)
select
  'telegram',
  a.customer_chat_id::text,
  a.customer_chat_id,
  (array_agg(a.customer_name order by a.created_at asc) filter (where a.customer_name is not null))[1],
  (array_agg(a.customer_username order by a.created_at desc) filter (where a.customer_username is not null))[1],
  min(a.created_at),
  max(a.created_at)
from public.conversation_archive a
where a.customer_chat_id is not null
group by a.customer_chat_id
on conflict (platform, external_id) do update set
  chat_id = coalesce(public.customer_contacts.chat_id, excluded.chat_id),
  display_name = coalesce(excluded.display_name, public.customer_contacts.display_name),
  username = coalesce(excluded.username, public.customer_contacts.username),
  first_seen_at = least(public.customer_contacts.first_seen_at, excluded.first_seen_at),
  last_seen_at = greatest(public.customer_contacts.last_seen_at, excluded.last_seen_at);

-- احتياطاً: أضف أيضاً الزبائن الذين لديهم اشتراك أو ربط قديم حتى لو لم يوجد
-- لهم صف كامل في conversation_archive.
insert into public.customer_contacts (platform, external_id, chat_id, display_name)
select 'telegram', r.customer_chat_id::text, r.customer_chat_id, r.customer_name
from public.subscription_reminders r
where r.customer_chat_id is not null
on conflict (platform, external_id) do nothing;

insert into public.customer_contacts (platform, external_id, chat_id)
select 'telegram', l.chat_id::text, l.chat_id
from public.totp_links l
where l.chat_id is not null
on conflict (platform, external_id) do nothing;

