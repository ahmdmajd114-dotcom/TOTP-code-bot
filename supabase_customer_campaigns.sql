-- حملات الرسائل للعملاء. شغّل الملف مرة واحدة في Supabase SQL Editor.
create table if not exists public.customer_campaigns (
  id uuid primary key default gen_random_uuid(),
  owner_user_id bigint not null,
  audience_key text not null,
  message_text text not null,
  status text not null default 'draft' check (status in ('draft', 'sending', 'sent', 'cancelled')),
  recipient_count integer not null default 0,
  sent_count integer not null default 0,
  failed_count integer not null default 0,
  skipped_count integer not null default 0,
  created_at timestamptz not null default now(),
  sent_at timestamptz
);

create table if not exists public.customer_campaign_recipients (
  id bigint generated always as identity primary key,
  campaign_id uuid not null references public.customer_campaigns(id) on delete cascade,
  customer_chat_id bigint not null,
  business_connection_id text,
  customer_name text,
  delivery_status text not null default 'pending' check (delivery_status in ('pending', 'sent', 'failed', 'skipped')),
  error_text text,
  delivered_at timestamptz,
  created_at timestamptz not null default now(),
  unique (campaign_id, customer_chat_id)
);

create index if not exists customer_campaign_recipients_pending_idx
  on public.customer_campaign_recipients (campaign_id, delivery_status);

alter table public.customer_campaigns enable row level security;
alter table public.customer_campaign_recipients enable row level security;
revoke all on table public.customer_campaigns from anon, authenticated;
revoke all on table public.customer_campaign_recipients from anon, authenticated;
grant select, insert, update, delete on table public.customer_campaigns to service_role;
grant select, insert, update, delete on table public.customer_campaign_recipients to service_role;
grant usage, select on sequence public.customer_campaign_recipients_id_seq to service_role;
