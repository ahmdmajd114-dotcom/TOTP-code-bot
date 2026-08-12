-- تدقيق وصل الدفع في فرع التفاعل فقط. لا نخزن الصورة ولا أي حسابات.
create table if not exists public.payment_proof_reviews (
  id bigint generated always as identity primary key,
  conversation_session_id text not null references public.conversation_sessions(id) on delete cascade,
  customer_chat_id bigint not null,
  selected_plan_id uuid references public.catalog_plans(id) on delete set null,
  expected_amount integer,
  detected_amount integer,
  decision text not null check (decision in ('approved', 'needs_review', 'rejected')),
  analysis jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists payment_proof_reviews_session_created_idx
  on public.payment_proof_reviews (conversation_session_id, created_at desc);

alter table public.payment_proof_reviews enable row level security;
revoke all on table public.payment_proof_reviews from anon, authenticated;
grant select, insert, update, delete on table public.payment_proof_reviews to service_role;
grant usage, select on sequence public.payment_proof_reviews_id_seq to service_role;
