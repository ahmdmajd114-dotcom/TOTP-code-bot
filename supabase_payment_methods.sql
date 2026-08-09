-- طرق الدفع المعتمدة. تبقى محمية بـRLS ويصل لها البوت من server-side فقط.
create table if not exists public.payment_methods (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  instructions text not null,
  is_active boolean not null default true,
  display_order smallint not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists payment_methods_active_order_idx
  on public.payment_methods (is_active, display_order, name);

alter table public.payment_methods enable row level security;
