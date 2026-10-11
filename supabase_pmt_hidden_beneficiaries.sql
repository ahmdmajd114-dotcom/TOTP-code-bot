-- حذف قابل للاسترجاع من واجهة قائمة PMT (لا يحذف المصدر الأصلي).
create table if not exists public.pmt_hidden_beneficiaries (
  beneficiary_key text primary key,
  hidden_at timestamptz not null default now()
);

alter table public.pmt_hidden_beneficiaries enable row level security;
revoke all on table public.pmt_hidden_beneficiaries from anon, authenticated;
grant select, insert, delete on table public.pmt_hidden_beneficiaries to service_role;
