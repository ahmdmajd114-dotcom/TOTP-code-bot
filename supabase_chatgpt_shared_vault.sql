-- خزينة حسابات ChatGPT المشتركة. الوصول للسيرفر فقط عبر service_role.
create table if not exists public.chatgpt_shared_accounts (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  password text not null,
  totp_secret text not null,
  capacity smallint not null default 3 check (capacity between 1 and 20),
  is_active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists public.chatgpt_account_assignments (
  id bigint generated always as identity primary key,
  account_id uuid not null references public.chatgpt_shared_accounts(id) on delete restrict,
  customer_chat_id bigint not null,
  conversation_session_id text references public.conversation_sessions(id) on delete set null,
  status text not null default 'active' check (status in ('active', 'revoked')),
  assigned_at timestamptz not null default now(),
  unique (customer_chat_id, conversation_session_id)
);

create index if not exists chatgpt_account_assignments_account_active_idx
  on public.chatgpt_account_assignments (account_id) where status = 'active';

alter table public.chatgpt_shared_accounts enable row level security;
alter table public.chatgpt_account_assignments enable row level security;
revoke all on table public.chatgpt_shared_accounts, public.chatgpt_account_assignments from anon, authenticated;
grant select, insert, update, delete on table public.chatgpt_shared_accounts, public.chatgpt_account_assignments to service_role;
grant usage, select on sequence public.chatgpt_account_assignments_id_seq to service_role;

-- الحسابات الموجودة أيضاً تتبع الحد الجديد عند تنفيذ هذا الملف في Supabase.
alter table public.chatgpt_shared_accounts alter column capacity set default 3;
update public.chatgpt_shared_accounts set capacity = 3 where capacity > 3;
