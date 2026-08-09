-- كتالوج منتجات وباقات البوت.
-- شغّل هذا الملف مرة واحدة في Supabase SQL Editor قبل استخدام شاشة الكتالوج.

create table if not exists public.catalog_products (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  aliases text[] not null default '{}',
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- للجدول المنشأ سابقاً قبل إضافة الكلمات البديلة.
alter table public.catalog_products
  add column if not exists aliases text[] not null default '{}';

create table if not exists public.catalog_plans (
  id uuid primary key default gen_random_uuid(),
  product_id uuid not null references public.catalog_products(id) on delete cascade,
  name text not null,
  price integer not null check (price >= 0),
  duration text,
  description text,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (product_id, name)
);

create index if not exists catalog_plans_product_id_idx
  on public.catalog_plans(product_id);

-- لا يحتاج البوت إلى RLS إذا كان SUPABASE_KEY هو service-role key.
-- إذا كنت تستخدم anon key، أضف policies مناسبة للأونر أو استخدم service-role
-- في متغيرات بيئة البوت فقط، ولا تشاركه مع أي تطبيق عمومي.
