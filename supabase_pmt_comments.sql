-- تعليقات داخلية مرتبطة بسجلات PMT. تُحفظ عبر خادم البوت فقط.
create table if not exists public.pmt_beneficiary_comments (
  beneficiary_key text primary key,
  comment text not null default '',
  updated_at timestamptz not null default now(),
  constraint pmt_beneficiary_comments_comment_length check (char_length(comment) <= 1000)
);

alter table public.pmt_beneficiary_comments enable row level security;
revoke all on table public.pmt_beneficiary_comments from anon, authenticated;
grant select, insert, update on table public.pmt_beneficiary_comments to service_role;
