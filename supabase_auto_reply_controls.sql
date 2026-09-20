-- زر الإيقاف/الاستئناف العام للردود التلقائية.
-- شغّل هذا الملف مرة واحدة في Supabase SQL Editor قبل استعمال الزر.

create table if not exists public.auto_reply_controls (
  owner_user_id bigint primary key,
  replies_paused boolean not null default false,
  updated_at timestamptz not null default now()
);

-- أثناء الإيقاف نحتفظ بآخر كلام مجمّع لكل زبون، ثم يعالجه البوت عند
-- الاستئناف. مفتاح مركب يمنع تكرار الرد على نفس الزبون عشرات المرات.
create table if not exists public.paused_customer_messages (
  owner_user_id bigint not null,
  customer_chat_id bigint not null,
  business_connection_id text not null,
  customer_name text,
  customer_username text,
  message_id bigint not null,
  message_text text not null,
  received_at timestamptz not null default now(),
  primary key (owner_user_id, customer_chat_id)
);

create index if not exists paused_customer_messages_received_idx
  on public.paused_customer_messages (owner_user_id, received_at);

alter table public.auto_reply_controls enable row level security;
alter table public.paused_customer_messages enable row level security;

revoke all on table public.auto_reply_controls from anon, authenticated;
revoke all on table public.paused_customer_messages from anon, authenticated;

grant select, insert, update, delete on table public.auto_reply_controls to service_role;
grant select, insert, update, delete on table public.paused_customer_messages to service_role;
