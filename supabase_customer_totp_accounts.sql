-- يسمح للزبون بامتلاك أكثر من حساب TOTP بدون فقدان الحساب القديم.
-- شغّل الملف مرة واحدة في Supabase SQL Editor قبل استعمال الميزة.

create table if not exists public.customer_totp_account_links (
  customer_chat_id bigint not null,
  account_id text not null,
  is_primary boolean not null default false,
  status text not null default 'active'
    check (status in ('active', 'replaced', 'cancelled')),
  linked_at timestamptz not null default now(),
  last_selected_at timestamptz,
  primary key (customer_chat_id, account_id)
);

create index if not exists customer_totp_account_links_customer_idx
  on public.customer_totp_account_links (customer_chat_id, status, linked_at desc);

-- يستورد الربطات القديمة كحساب أساسي، من دون أن يغير جدول النظام القديم.
insert into public.customer_totp_account_links (
  customer_chat_id, account_id, is_primary, status, linked_at
)
select
  chat_id,
  account_id::text,
  true,
  'active',
  coalesce(linked_at, now())
from public.totp_links
on conflict (customer_chat_id, account_id) do nothing;

alter table public.customer_totp_account_links enable row level security;
revoke all on table public.customer_totp_account_links from anon, authenticated;
grant select, insert, update, delete on table public.customer_totp_account_links to service_role;
