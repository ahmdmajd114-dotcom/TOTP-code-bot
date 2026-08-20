-- تنبيهات انتهاء كل الاشتراكات ورسالة Feedback تلقائية بعد النهاية.
-- شغّل هذا الملف مرة واحدة في Supabase SQL Editor.
create table if not exists public.subscription_reminders (
  id bigint generated always as identity primary key,
  customer_chat_id bigint,
  business_connection_id text,
  customer_name text not null,
  customer_username text,
  subscription_type text not null default 'general',
  duration_months smallint,
  duration_days integer,
  product_name text,
  plan_name text,
  plan_duration text,
  feedback_only boolean not null default false,
  feedback_status text not null default 'none',
  feedback_requested_at timestamptz,
  feedback_responded_at timestamptz,
  is_debt boolean not null default false,
  started_at timestamptz not null default now(),
  expires_at timestamptz not null,
  status text not null default 'active' check (status in ('active', 'expired', 'cancelled')),
  expiry_notified_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists subscription_reminders_due_idx
  on public.subscription_reminders (expires_at) where status = 'active';

-- توافق إذا كان الجدول قد شُغّل قبل إضافة التسجيل اليدوي.
alter table public.subscription_reminders
  alter column customer_chat_id drop not null;

alter table public.subscription_reminders
  add column if not exists is_debt boolean not null default false;

alter table public.subscription_reminders
  add column if not exists duration_days integer;
alter table public.subscription_reminders
  add column if not exists business_connection_id text;
alter table public.subscription_reminders
  add column if not exists product_name text;
alter table public.subscription_reminders
  add column if not exists plan_name text;
alter table public.subscription_reminders
  add column if not exists plan_duration text;
alter table public.subscription_reminders
  add column if not exists feedback_only boolean not null default false;
alter table public.subscription_reminders
  add column if not exists feedback_status text not null default 'none';
alter table public.subscription_reminders
  add column if not exists feedback_requested_at timestamptz;
alter table public.subscription_reminders
  add column if not exists feedback_responded_at timestamptz;
alter table public.subscription_reminders
  alter column subscription_type set default 'general';

-- النسخ القديمة كانت تمنع إلا private/shared وشهر/شهرين.
alter table public.subscription_reminders
  drop constraint if exists subscription_reminders_subscription_type_check;
alter table public.subscription_reminders
  drop constraint if exists subscription_reminders_duration_months_check;
alter table public.subscription_reminders
  alter column duration_months drop not null;

alter table public.subscription_reminders enable row level security;
revoke all on table public.subscription_reminders from anon, authenticated;
grant select, insert, update, delete on table public.subscription_reminders to service_role;
grant usage, select on sequence public.subscription_reminders_id_seq to service_role;

-- ترحيل مرة واحدة للزبائن القدامى المرتبطين حالياً بـ /link:
-- نعطي كل واحد اشتراكاً مشتركاً لمدة شهرين من وقت تشغيل هذا الملف، حتى
-- يبقى حق طلب الكود متاحاً ولا تحتاج تضيفهم يدوياً واحداً واحداً.
insert into public.subscription_reminders (
  customer_chat_id, customer_name, subscription_type, duration_months,
  started_at, expires_at, status
)
select
  link.chat_id,
  'زبون قديم',
  'shared',
  2,
  now(),
  now() + interval '60 days',
  'active'
from public.totp_links as link
where not exists (
  select 1
  from public.subscription_reminders as reminder
  where reminder.customer_chat_id = link.chat_id
    and reminder.status = 'active'
    and reminder.expires_at > now()
);
