-- يحفظ آخر صورة مرسلة من كل عميل حتى يعمل اختصار AC بدون Reply.
-- شغّل هذا الملف مرة واحدة في Supabase SQL Editor.
create table if not exists public.latest_customer_business_photos (
  customer_chat_id bigint primary key,
  business_connection_id text not null,
  photo_file_id text not null,
  customer_name text,
  customer_username text,
  message_id integer,
  received_at timestamptz not null default now()
);

alter table public.latest_customer_business_photos enable row level security;
revoke all on table public.latest_customer_business_photos from anon, authenticated;
grant select, insert, update, delete on table public.latest_customer_business_photos to service_role;
