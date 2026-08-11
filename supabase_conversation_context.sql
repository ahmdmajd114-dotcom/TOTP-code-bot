-- المرحلة الأولى من وكيل الذكاء: بناء جلسات وسياق للمحادثات فقط.
-- لا يحتوي هذا الملف على صلاحيات عامة: البوت الخلفي وحده يستخدم service_role.

create table if not exists public.conversation_sessions (
  id text primary key,
  customer_chat_id bigint not null,
  customer_name text,
  customer_username text,
  status text not null default 'open' check (status in ('open', 'closed')),
  latest_stage text not null default 'observing',
  message_count integer not null default 0 check (message_count >= 0),
  started_at timestamptz not null default now(),
  last_activity_at timestamptz not null default now(),
  closed_at timestamptz,
  source text not null default 'live' check (source in ('live', 'interactive', 'legacy_archive')),
  created_at timestamptz not null default now()
);

create index if not exists conversation_sessions_customer_activity_idx
  on public.conversation_sessions (customer_chat_id, last_activity_at desc);

create table if not exists public.conversation_context_events (
  id bigint generated always as identity primary key,
  conversation_session_id text not null references public.conversation_sessions(id) on delete cascade,
  archive_message_id integer references public.conversation_archive(id) on delete set null,
  sender_type text not null check (sender_type in ('customer', 'owner', 'bot')),
  event_type text not null,
  event_data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (archive_message_id, event_type)
);

create index if not exists conversation_context_events_session_created_idx
  on public.conversation_context_events (conversation_session_id, created_at);

alter table public.conversation_sessions enable row level security;
alter table public.conversation_context_events enable row level security;

revoke all on table public.conversation_sessions from anon, authenticated;
revoke all on table public.conversation_context_events from anon, authenticated;
grant select, insert, update, delete on table public.conversation_sessions to service_role;
grant select, insert, update, delete on table public.conversation_context_events to service_role;
grant usage, select on sequence public.conversation_context_events_id_seq to service_role;

-- نحتفظ بالسياقات القديمة للرجوع والتحليل، لكن نعلّمها كمؤرشفة ومغلقة.
-- أما أي محادثة جديدة فستستخدم فاصل السكوت الجديد من كود البوت.
insert into public.conversation_sessions (
  id, customer_chat_id, customer_name, customer_username, status, latest_stage,
  message_count, started_at, last_activity_at, closed_at, source
)
select
  a.conversation_session_id,
  min(a.customer_chat_id),
  (array_agg(a.customer_name order by a.created_at desc) filter (where a.customer_name is not null))[1],
  (array_agg(a.customer_username order by a.created_at desc) filter (where a.customer_username is not null))[1],
  'closed',
  'observing',
  count(*)::integer,
  min(a.created_at),
  max(a.created_at),
  max(a.created_at),
  'legacy_archive'
from public.conversation_archive a
where a.conversation_session_id is not null
group by a.conversation_session_id
on conflict (id) do nothing;
