-- ردود فرع التفاعل: الـAI يختار action_key فقط، والنص يخرج ثابتاً من هنا.
create table if not exists public.interactive_response_templates (
  action_key text primary key,
  label text not null,
  response_text text not null,
  is_active boolean not null default true,
  updated_at timestamptz not null default now()
);

alter table public.interactive_response_templates enable row level security;
revoke all on table public.interactive_response_templates from anon, authenticated;
grant select, insert, update, delete on table public.interactive_response_templates to service_role;

insert into public.interactive_response_templates (action_key, label, response_text)
values
  ('greeting', 'تحية', 'وعليكم السلام ورحمة الله وبركاته اهلا وسهلا'),
  ('chatgpt_plans', 'باقات ChatGPT', ''),
  ('payment_methods', 'إرسال طرق الدفع', ''),
  ('request_plan_choice', 'طلب اختيار الباقة', 'تدلل، اختار الباقة اللي تناسبك حتى نكمل.'),
  ('request_payment_proof', 'طلب صورة التحويل', 'بلا زحمة عليك دزلي صورة التحويل حتى أتأكد منها.'),
  ('payment_under_review', 'مراجعة التحويل', 'وصلتني الصورة، دا أتأكد من التحويل هسه.'),
  ('request_support_screenshot', 'طلب سكرين للمشكلة', 'بلا زحمة دزلي سكرين للمشكلة حتى أشوفها وأساعدك.'),
  ('registration_guidance', 'إرشاد التسجيل', 'طريقة التسجيل موجودة ويا تفاصيل الحساب، وإذا تطلعلك خطوة ما واضحة دزلي سكرين إلها.'),
  ('code_request', 'طلب كود', 'تدلل، لحظة وأدزلك الكود.'),
  ('closing', 'ختام وشكر', 'أهلين وسهلين، تدلل.'),
  ('clarify', 'سؤال توضيحي', 'تدلل، وضحلي شنو تريد بالضبط حتى أساعدك.'),
  ('handoff', 'حالة تحتاج متابعة', 'تدلل، خليني أتأكد من الموضوع وأرجعلك.')
on conflict (action_key) do nothing;
