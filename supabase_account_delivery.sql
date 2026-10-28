-- Managed ChatGPT delivery, installed explicitly. No migration of money or
-- inferred subscription dates. Requires financial + subscription schemas.
begin;
create table if not exists public.delivery_accounts(
 id uuid primary key default gen_random_uuid(), owner_id bigint not null,
 native_id uuid not null references chatgpt_shared_accounts(id), legacy_id text not null,
 code text not null unique, enabled boolean not null default true,
 revision integer not null default 1, created_at timestamptz not null default now(),
 unique(native_id), unique(legacy_id)
);
create table if not exists public.delivery_templates(
 owner_id bigint primary key,
 guide_url text not null default 'https://t.me/+IcHCjNi8_ilkZjdi',
 channel_url text not null default 'https://t.me/+M8XsrznhCNJkNTA6',
 terms_url text not null default 'https://totp-code-bot.onrender.com/terms',
 revision integer not null default 1
);
create table if not exists public.delivery_legacy_holds(
 account_id uuid not null references delivery_accounts(id), customer_id bigint not null,
 reviewed boolean not null default false, primary key(account_id,customer_id)
);
create table if not exists public.delivery_contracts(
 id uuid primary key default gen_random_uuid(), owner_id bigint not null,
 customer_id bigint not null, payer_id bigint not null, customer_name text not null,
 account_id uuid not null references delivery_accounts(id), seats integer not null check(seats between 1 and 20),
 reminder_id bigint references subscription_reminders(id),
 starts_at timestamptz not null, ends_at timestamptz not null,
 status text not null default 'active' check(status in ('active','cancelled')),
 revision integer not null default 1, created_at timestamptz not null default now(),
 check(ends_at>starts_at)
);
create table if not exists public.delivery_requests(
 id uuid primary key, owner_id bigint not null, customer_id bigint not null,
 customer_name text not null, business_id text not null,
 account_id uuid not null references delivery_accounts(id),
 command_message_id bigint not null, state jsonb not null default '{}',
 revision integer not null default 1,
 status text not null default 'draft' check(status in ('draft','reserved','sending','uncertain','sent','complete','cancelled','failed')),
 hold_until timestamptz, sent_message_id bigint, sent_at timestamptz,
 result jsonb, created_at timestamptz not null default now(),
 unique(owner_id,business_id,customer_id,command_message_id)
);
create table if not exists public.delivery_periods(
 id uuid primary key default gen_random_uuid(), owner_id bigint not null,
 contract_id uuid not null references delivery_contracts(id),
 request_id uuid not null unique references delivery_requests(id),
 funding_id text, allocated bigint not null default 0 check(allocated>=0),
 starts_at timestamptz not null, ends_at timestamptz not null,
 kind text not null check(kind in ('new','renew','replace','comp','adopt')),
 voided boolean not null default false, snapshot jsonb not null default '{}'
);
alter table delivery_periods add column if not exists recorded_at timestamptz not null default clock_timestamp();
create index if not exists delivery_contract_customer on delivery_contracts(owner_id,customer_id);
create unique index if not exists delivery_one_contract_per_reminder on delivery_contracts(reminder_id) where reminder_id is not null;
create index if not exists delivery_holds on delivery_requests(account_id,status,hold_until);
create table if not exists public.delivery_events(
 id bigint generated always as identity primary key, owner_id bigint not null,
 request_id uuid, kind text not null, details jsonb not null default '{}', at timestamptz not null default now()
);

create or replace function public.delivery_occupancy(p_account uuid,p_exclude uuid default null)
returns jsonb language sql stable set search_path=public as $$
 with managed as (
   select customer_id,seats from delivery_contracts c where c.account_id=p_account and c.status='active' and c.ends_at>now()
     and c.id is distinct from p_exclude
 ), unmanaged as (
   select distinct customer_id from (
     select l.customer_chat_id as customer_id from customer_totp_account_links l join delivery_accounts a on a.legacy_id=l.account_id
       where a.id=p_account and l.status='active'
     union select l.chat_id from totp_links l join delivery_accounts a on a.legacy_id=l.account_id::text where a.id=p_account
     union select l.customer_chat_id from chatgpt_account_assignments l join delivery_accounts a on a.native_id=l.account_id
       where a.id=p_account and l.status='active'
   ) links where not exists(select 1 from delivery_contracts c where c.account_id=p_account and c.customer_id=links.customer_id)
   union select customer_id from delivery_legacy_holds where account_id=p_account and not reviewed
 ), holds as (
   select coalesce(sum((state->>'seats')::integer),0) n from delivery_requests where account_id=p_account
     and status in ('reserved','sending','uncertain','sent')
     and (status in ('sending','uncertain','sent') or hold_until>now())
     and coalesce((state->>'seat_delta')::integer,0)>0
 ) select jsonb_build_object('people',(select count(distinct customer_id) from (select customer_id from managed union select customer_id from unmanaged)x),
   'seats',coalesce((select sum(seats) from managed),0)+(select count(*) from unmanaged),
   'unreviewed',(select count(*) from unmanaged),'held',(select n from holds));
$$;

create or replace function public.delivery_manage(p_owner bigint,p_action text,p jsonb default '{}')
returns jsonb language plpgsql set search_path=public as $$
declare a delivery_accounts%rowtype; n chatgpt_shared_accounts%rowtype;
 r delivery_requests%rowtype; c delivery_contracts%rowtype; rem subscription_reminders%rowtype;
 op finance_operations%rowtype; oldp delivery_periods%rowtype; cfg delivery_templates%rowtype;
 rid uuid; target uuid; cid bigint; payer bigint; days integer; seat_count integer;
 duration integer; funding text; mode text; allocated bigint; used bigint; occ jsonb; legacy text; migration_id uuid;
 start_time timestamptz; end_time timestamptz; saved jsonb; outcome jsonb; rows_count integer; was_complete boolean:=false;
begin
 perform pg_advisory_xact_lock(p_owner);
 if p_action='register' then
   if coalesce(p->>'email','')!~'^[^ @]+@[^ @]+\.[^ @]+$' or length(coalesce(p->>'password',''))<6
     or coalesce(p->>'secret','')!~'^[A-Z2-7]{16,128}$' or coalesce(p->>'code','')!~'^[A-Za-z0-9_-]{4,40}$' then
     raise exception 'Invalid account fields'; end if;
   select * into n from chatgpt_shared_accounts where lower(email)=lower(p->>'email') for update;
   if found then
     if n.password<>p->>'password' or n.totp_secret<>p->>'secret' then raise exception 'Existing credentials differ; review before update';end if;
   else
     insert into chatgpt_shared_accounts(email,password,totp_secret,capacity)
     values(p->>'email',p->>'password',p->>'secret',(p->>'capacity')::integer) returning * into n;
   end if;
   if exists(select 1 from delivery_accounts where native_id=n.id) then
     select * into a from delivery_accounts where native_id=n.id;
     return jsonb_build_object('id',a.id,'code',a.code,'duplicate',true);
   end if;
   select count(*) into rows_count from totp_accounts where lower(label)=lower(n.email);
   if rows_count>1 then raise exception 'Multiple legacy accounts match email; review required';end if;
   select id::text into legacy from totp_accounts where lower(label)=lower(n.email);
   if legacy is null then
     insert into totp_accounts(label,secret,link_code) values(n.email,n.totp_secret,p->>'code') returning id::text into legacy;
   elsif exists(select 1 from totp_accounts where id::text=legacy and secret<>n.totp_secret) then
     raise exception 'Legacy TOTP differs; review required';
   end if;
   insert into delivery_accounts(owner_id,native_id,legacy_id,code) values(p_owner,n.id,legacy,p->>'code') returning * into a;
   insert into delivery_legacy_holds(account_id,customer_id)
     select a.id,customer_id from (
       select customer_chat_id customer_id from customer_totp_account_links where account_id=legacy and status='active'
       union select chat_id from totp_links where account_id::text=legacy
       union select customer_chat_id from chatgpt_account_assignments where account_id=n.id and status='active'
     ) old_links on conflict do nothing;
   insert into delivery_events(owner_id,kind,details) values(p_owner,'register',jsonb_build_object('account_id',a.id));
   return jsonb_build_object('id',a.id,'code',a.code);
 end if;
 if p_action='list' then
   return jsonb_build_object('accounts',coalesce((select jsonb_agg(jsonb_build_object('id',da.id,'code',da.code,'email',na.email,'capacity',na.capacity,'enabled',da.enabled and na.is_active,'revision',da.revision,'occupancy',delivery_occupancy(da.id)))
     from delivery_accounts da join chatgpt_shared_accounts na on na.id=da.native_id where da.owner_id=p_owner),'[]'::jsonb),
     'history',coalesce((select jsonb_agg(jsonb_build_object('id',id,'customer_id',customer_id,'status',status,'created_at',created_at) order by created_at desc) from delivery_requests where owner_id=p_owner),'[]'::jsonb),
     'legacy_holds',coalesce((select jsonb_agg(jsonb_build_object('account_id',h.account_id,'customer_id',h.customer_id)) from delivery_legacy_holds h join delivery_accounts da on da.id=h.account_id where da.owner_id=p_owner and not h.reviewed),'[]'::jsonb),
     'pending',coalesce((select jsonb_agg(jsonb_build_object('id',id,'customer_id',customer_id,'status',status)) from delivery_requests where owner_id=p_owner and status not in ('complete','cancelled','failed')),'[]'::jsonb));
 end if;
 if p_action='account' then
   select * into a from delivery_accounts where id=(p->>'id')::uuid and owner_id=p_owner for update;
   if not found or a.revision is distinct from (p->>'revision')::integer then raise exception 'Account changed; review';end if;
   if p ? 'enabled' then update delivery_accounts set enabled=(p->>'enabled')::boolean,revision=revision+1 where id=a.id;end if;
   if p ? 'capacity' then
     occ:=delivery_occupancy(a.id);
     if (p->>'capacity')::integer<1 or (p->>'capacity')::integer>20 or (p->>'capacity')::integer<(occ->>'seats')::integer+(occ->>'held')::integer then raise exception 'Capacity below active reservations';end if;
     update chatgpt_shared_accounts set capacity=(p->>'capacity')::integer where id=a.native_id;
     update delivery_accounts set revision=revision+1 where id=a.id;
   end if;
   insert into delivery_events(owner_id,kind,details) values(p_owner,'account_update',jsonb_build_object('account_id',a.id,'fields',p-'id'-'revision'));
   return jsonb_build_object('saved',true);
 end if;
 if p_action='release_access' then
   select * into a from delivery_accounts where id=(p->>'id')::uuid and owner_id=p_owner for update;
   if not found then raise exception 'Account missing';end if;
   if exists(select 1 from delivery_contracts where account_id=a.id and customer_id=(p->>'customer_id')::bigint and status='active' and ends_at>now()) then raise exception 'Customer still has active subscription';end if;
   update delivery_legacy_holds set reviewed=true where account_id=a.id and customer_id=(p->>'customer_id')::bigint;
   update customer_totp_account_links set status='cancelled',is_primary=false where customer_chat_id=(p->>'customer_id')::bigint and account_id=a.legacy_id;
   delete from totp_links where chat_id=(p->>'customer_id')::bigint and account_id::text=a.legacy_id;
   update chatgpt_account_assignments set status='revoked' where account_id=a.native_id and customer_chat_id=(p->>'customer_id')::bigint and status='active';
   insert into delivery_events(owner_id,kind,details) values(p_owner,'owner_confirmed_logout',jsonb_build_object('account_id',a.id,'customer_id',p->'customer_id'));
   return jsonb_build_object('saved',true);
 end if;
 if p_action='credentials' then
   select * into a from delivery_accounts where id=(p->>'id')::uuid and owner_id=p_owner for update;
   if not found or a.revision is distinct from (p->>'revision')::integer then raise exception 'Account changed; review';end if;
   if length(coalesce(p->>'password',''))<6 or coalesce(p->>'secret','')!~'^[A-Z2-7]{16,128}$' then raise exception 'Invalid credentials';end if;
   if p ? 'email' and p->>'email'!~'^[^ @]+@[^ @]+\.[^ @]+$' then raise exception 'Invalid email';end if;
   if exists(select 1 from delivery_requests where account_id=a.id and status in ('sending','uncertain','sent')) then raise exception 'Account has uncertain delivery; resolve first';end if;
   update chatgpt_shared_accounts set password=p->>'password',totp_secret=p->>'secret',email=coalesce(p->>'email',email) where id=a.native_id;
   update totp_accounts set secret=p->>'secret',label=coalesce(p->>'email',label) where id::text=a.legacy_id;
   update delivery_accounts set revision=revision+1 where id=a.id;
   insert into delivery_events(owner_id,kind,details) values(p_owner,'credentials_updated',jsonb_build_object('account_id',a.id,'affected',delivery_occupancy(a.id)));
   return jsonb_build_object('saved',true);
 end if;
 if p_action='template' then
   insert into delivery_templates(owner_id) values(p_owner) on conflict do nothing;
   return to_jsonb((select t from delivery_templates t where owner_id=p_owner));
 end if;
 if p_action='save_template' then
   if p->>'guide_url'!~'^https://t.me/[^[:space:]]+$' or p->>'channel_url'!~'^https://t.me/[^[:space:]]+$' then raise exception 'Invalid Telegram URL';end if;
   insert into delivery_templates(owner_id,guide_url,channel_url) values(p_owner,p->>'guide_url',p->>'channel_url')
     on conflict(owner_id) do update set guide_url=excluded.guide_url,channel_url=excluded.channel_url,revision=delivery_templates.revision+1;
   return jsonb_build_object('saved',true);
 end if;
 if p_action='start' then
   select da.* into a from delivery_accounts da join chatgpt_shared_accounts na on na.id=da.native_id
    where da.owner_id=p_owner and
     (da.code=p->>'code' or da.legacy_id in (select id::text from totp_accounts where link_code=p->>'code'));
   if not found then return jsonb_build_object('managed',false);end if;
   insert into delivery_requests(id,owner_id,customer_id,customer_name,business_id,account_id,command_message_id)
     values((p->>'id')::uuid,p_owner,(p->>'customer_id')::bigint,p->>'customer_name',p->>'business_id',a.id,(p->>'message_id')::bigint)
     on conflict(owner_id,business_id,customer_id,command_message_id) do nothing;
   select * into r from delivery_requests where owner_id=p_owner and business_id=p->>'business_id' and customer_id=(p->>'customer_id')::bigint and command_message_id=(p->>'message_id')::bigint;
 elsif p_action in ('get','edit','reserve','claim','sent','finish','cancel','release','resolve_sent','resolve_unsent','undo','import_reminder') then
   select * into r from delivery_requests where id=(p->>'id')::uuid and owner_id=p_owner for update;
   if not found then raise exception 'Request missing';end if;
 else raise exception 'Unknown delivery action';end if;
 select * into a from delivery_accounts where id=r.account_id for update;
 select * into n from chatgpt_shared_accounts where id=a.native_id for update;
 if p_action='import_reminder' then
   if r.status<>'draft' then raise exception 'Card changed';end if;
   select * into rem from subscription_reminders where id=(p->>'reminder_id')::bigint and customer_chat_id=r.customer_id and status='active' and expires_at>now() for update;
   if not found or exists(select 1 from delivery_contracts where reminder_id=rem.id) then raise exception 'Legacy reminder unavailable';end if;
   if not exists(select 1 from customer_totp_account_links where customer_chat_id=r.customer_id and account_id=a.legacy_id and status='active')
     and not exists(select 1 from totp_links where chat_id=r.customer_id and account_id::text=a.legacy_id) then raise exception 'Legacy account relationship not proven';end if;
   if rem.product_name not in ('ChatGPT','جات','شات جي بي تي') then raise exception 'Legacy reminder is not ChatGPT';end if;
   insert into delivery_contracts(owner_id,customer_id,payer_id,customer_name,account_id,seats,reminder_id,starts_at,ends_at)
     values(p_owner,r.customer_id,r.customer_id,r.customer_name,a.id,1,rem.id,rem.started_at,rem.expires_at) returning * into c;
   migration_id:=gen_random_uuid();
   insert into delivery_requests(id,owner_id,customer_id,customer_name,business_id,account_id,command_message_id,state,status,result)
     values(migration_id,p_owner,r.customer_id,r.customer_name,r.business_id,a.id,-rem.id,
       '{"mode":"adopt"}'::jsonb,'complete',jsonb_build_object('contract_id',c.id,'reminder_id',rem.id,'ends_at',c.ends_at));
   select * into op from finance_operations where owner_id=p_owner and id=rem.financial_operation_id and kind in ('sale','debt_issue');
   insert into delivery_periods(owner_id,contract_id,request_id,funding_id,allocated,starts_at,ends_at,kind,snapshot)
     values(p_owner,c.id,migration_id,op.id,coalesce(op.sales,0),c.starts_at,c.ends_at,'adopt',jsonb_build_object('reminder_id',rem.id));
   update delivery_legacy_holds set reviewed=true where account_id=a.id and customer_id=r.customer_id;
   insert into delivery_events(owner_id,request_id,kind,details) values(p_owner,r.id,'reviewed_legacy_reminder',jsonb_build_object('reminder_id',rem.id));
 end if;
 if p_action='edit' then
   if r.status not in ('draft','failed') or (p->>'revision')::integer<>r.revision then raise exception 'Card changed; reload';end if;
   update delivery_requests set state=p->'state',revision=revision+1,status='draft' where id=r.id returning * into r;
 end if;
 if p_action='reserve' then
   if r.status='reserved' and r.hold_until>now() then null;
   elsif r.status not in ('draft','failed','reserved') then raise exception 'Review delivery status first';
   else
     if not a.enabled or not n.is_active then raise exception 'Account disabled';end if;
     mode=r.state->>'mode';target=(r.state->>'target')::uuid;
     cid=r.customer_id;payer=coalesce((r.state->>'payer_id')::bigint,cid);
     seat_count=coalesce((r.state->>'seats')::integer,1);
     if seat_count<1 or seat_count>20 or mode not in ('new','renew','replace','comp','resend') then raise exception 'Invalid delivery choice';end if;
     if mode<>'new' then
       select * into c from delivery_contracts where id=target and owner_id=p_owner and customer_id=cid for update;
       if not found or c.status<>'active' then raise exception 'Select exact subscription';end if;
       if exists(select 1 from delivery_requests x where x.id<>r.id and x.owner_id=p_owner and x.state->>'target'=target::text and x.status in ('reserved','sending','uncertain','sent') and (x.hold_until>now() or x.status<>'reserved')) then raise exception 'Subscription has pending delivery';end if;
       if (r.state->>'target_revision')::integer is distinct from c.revision then raise exception 'Subscription changed; reload';end if;
       if mode in ('renew','resend') and c.account_id<>a.id then raise exception 'Renewal requires same account';end if;
       if mode='resend' and c.ends_at<=now() then raise exception 'Subscription expired';end if;
       if mode='replace' and c.ends_at<=now() then raise exception 'Subscription expired; choose renewal or compensation';end if;
       seat_count=c.seats;
     end if;
     funding=r.state->>'funding_id';allocated=coalesce((r.state->>'allocated')::bigint,0);
     if mode in ('new','renew') then
       if funding is null then raise exception 'Select payment or explicit debt';end if;
       if not exists(select 1 from catalog_plans where id::text=r.state->>'plan_id' and is_active and product_id::text=r.state->>'product_id') then raise exception 'Plan changed; review';end if;
       if funding='debt' then
         select price into allocated from catalog_plans where id::text=r.state->>'plan_id' and is_active;
         if allocated is null or allocated<=0 then raise exception 'Plan missing';end if;
       else
         select * into op from finance_operations where owner_id=p_owner and id=funding and customer_id=payer and kind in ('sale','debt_issue') and not historical;
         if not found or exists(select 1 from finance_operations where owner_id=p_owner and kind='reversal' and reference_id=op.id) then raise exception 'Payment missing or reversed';end if;
         if op.payload->>'product_id' is not null and op.payload->>'product_id' is distinct from r.state->>'product_id' then raise exception 'Payment product differs';end if;
         if exists(select 1 from finance_operations where owner_id=p_owner and kind='refund' and reference_id=op.id) then raise exception 'Payment has refund; review';end if;
         select coalesce(sum(dp.allocated),0) into used from delivery_periods dp where dp.owner_id=p_owner and dp.funding_id=funding and not dp.voided;
         used:=used+coalesce((select sum((state->>'allocated')::bigint) from delivery_requests x where owner_id=p_owner and x.id<>r.id and state->>'funding_id'=funding and status in ('reserved','sending','uncertain','sent') and (status<>'reserved' or hold_until>now())),0);
         if allocated<=0 or used+allocated>op.sales then raise exception 'Payment allocation already used';end if;
         if exists(select 1 from subscription_reminders where financial_operation_id=funding and status='active' and expires_at>now()
             and id not in (select reminder_id from delivery_contracts where reminder_id is not null)) then raise exception 'Legacy payment already activated; review subscription';end if;
       end if;
       days=(r.state->>'days')::integer;
       if days is null or days<1 or days>3650 then raise exception 'Invalid duration';end if;
     elsif mode='comp' then
       days=(r.state->>'days')::integer;
       if days is null or days<1 or days>3650 then raise exception 'Invalid compensation days';end if;
     end if;
     -- Do not count this request's expired reservation twice.
     update delivery_requests set status='draft' where id=r.id;
     occ:=delivery_occupancy(a.id,case when mode in ('replace','comp','renew','resend','adopt') then target else null end);
     if mode not in ('renew','resend') and (occ->>'seats')::integer+(occ->>'held')::integer+seat_count>n.capacity then raise exception 'Account capacity exceeded';end if;
     if mode='renew' and not exists(select 1 from delivery_contracts where id=target and ends_at>now()) and
       (occ->>'seats')::integer+(occ->>'held')::integer+seat_count>n.capacity then raise exception 'Account capacity exceeded';end if;
     insert into delivery_templates(owner_id) values(p_owner) on conflict do nothing;
     select * into cfg from delivery_templates where owner_id=p_owner;
     update delivery_requests set status='reserved',hold_until=now()+interval '15 minutes',revision=revision+1,
       state=state||jsonb_build_object('seats',seat_count,'seat_delta',case when mode='resend' or (target is not null and c.account_id=a.id and c.ends_at>now()) then 0 else seat_count end,
          'allocated',allocated,'account_revision',a.revision,'template',to_jsonb(cfg),
          'credentials_hash',md5(n.email||n.password||n.totp_secret),'target_before',case when target is not null then to_jsonb(c) else null end)
       where id=r.id returning * into r;
   end if;
 end if;
 if p_action='claim' then
   if r.status<>'reserved' or r.hold_until<=now() then raise exception 'Reservation expired or already sending';end if;
   if not a.enabled or not n.is_active or r.state->>'credentials_hash'<>md5(n.email||n.password||n.totp_secret) then raise exception 'Account changed; review';end if;
   occ:=delivery_occupancy(a.id);
   if (occ->>'seats')::integer+(occ->>'held')::integer>n.capacity then raise exception 'Account capacity exceeded';end if;
   if r.state->>'target' is not null and not exists(select 1 from delivery_contracts where id=(r.state->>'target')::uuid and revision=(r.state->>'target_revision')::integer) then raise exception 'Subscription changed; reload';end if;
   update delivery_requests set status='sending',revision=revision+1 where id=r.id returning * into r;
 end if;
 if p_action in ('sent','resolve_sent') then
   if r.status not in ('sending','uncertain','sent') then raise exception 'Not awaiting delivery confirmation';end if;
   update delivery_requests set status='sent',sent_message_id=coalesce((p->>'message_id')::bigint,sent_message_id),sent_at=coalesce(sent_at,now()) where id=r.id returning * into r;
 end if;
 if p_action='resolve_unsent' then
   if r.status not in ('sending','uncertain') then raise exception 'Not uncertain';end if;
   update delivery_requests set status='draft',hold_until=null,revision=revision+1 where id=r.id returning * into r;
   insert into delivery_events(owner_id,request_id,kind) values(p_owner,r.id,'owner_confirmed_not_received');
 end if;
 if p_action='cancel' then
   if r.status not in ('draft','reserved','failed') then raise exception 'Possible credential exposure; resolve delivery before cancellation';end if;
   update delivery_requests set status='cancelled',hold_until=null where id=r.id returning * into r;
 end if;
 if p_action='release' then
   if r.status<>'reserved' then raise exception 'Reservation changed';end if;
   update delivery_requests set status='draft',hold_until=null,revision=revision+1 where id=r.id returning * into r;
 end if;
 was_complete:=r.status='complete';
 if p_action='finish' and not was_complete then
   if r.status<>'sent' then raise exception 'Delivery not confirmed';end if;
   mode=r.state->>'mode';target=(r.state->>'target')::uuid;funding=r.state->>'funding_id';payer=coalesce((r.state->>'payer_id')::bigint,r.customer_id);
   days=coalesce((r.state->>'days')::integer,0);seat_count=(r.state->>'seats')::integer;allocated=coalesce((r.state->>'allocated')::bigint,0);
   if target is not null then
     select * into c from delivery_contracts where id=target for update;
     if c.revision is distinct from (r.state->>'target_revision')::integer then raise exception 'Subscription changed after send; manual review required';end if;
   end if;
   if mode in ('new','renew') and funding='debt' then
     funding:='DEL-DEBT-'||r.id;
     saved:=jsonb_build_object('id',funding,'kind','debt_issue','occurred_at',r.sent_at,'customer_id',payer,'customer_name',case when payer=r.customer_id then r.customer_name else coalesce(r.state->>'payer_name',payer::text) end,
       'product',r.state->>'product','product_id',r.state->>'product_id','plan_id',r.state->>'plan_id','plan_name',r.state->>'plan_name',
       'sales',allocated,'new_debt',allocated,'payments','[]'::jsonb);
     perform finance_post(p_owner,saved);
   elsif mode in ('new','renew') then
     if exists(select 1 from finance_operations where owner_id=p_owner and kind in ('reversal','refund') and reference_id=funding) then raise exception 'Funding changed after sending; review';end if;
   end if;
   start_time:=r.sent_at;end_time:=start_time+make_interval(days=>days);
   if mode='renew' then start_time:=greatest(c.ends_at,r.sent_at);end_time:=start_time+make_interval(days=>days);end if;
   if mode in ('replace','adopt','resend') then start_time:=c.starts_at;end_time:=c.ends_at;end if;
   if mode='new' then
     insert into delivery_contracts(owner_id,customer_id,payer_id,customer_name,account_id,seats,starts_at,ends_at)
       values(p_owner,r.customer_id,payer,r.customer_name,a.id,seat_count,start_time,end_time) returning * into c;
   elsif mode<>'resend' then
     update delivery_contracts set account_id=a.id,starts_at=case when mode='renew' then starts_at else start_time end,
       ends_at=end_time,revision=revision+1 where id=c.id returning * into c;
   end if;
   if mode<>'resend' then
     if c.reminder_id is null then
       -- Consume an existing pending reminder only for a whole, unsplit sale.
       select * into rem from subscription_reminders where financial_operation_id=funding and status='pending_delivery' and customer_chat_id=r.customer_id
         and not exists(select 1 from delivery_contracts where reminder_id=subscription_reminders.id) limit 1;
       if found then
         update subscription_reminders set status='active',feedback_status='none',started_at=start_time,expires_at=end_time where id=rem.id;
       else
         insert into subscription_reminders(customer_chat_id,customer_name,business_connection_id,subscription_type,duration_days,product_name,plan_name,plan_duration,is_debt,started_at,expires_at,status)
           values(r.customer_id,r.customer_name,r.business_id,'shared',days,coalesce(r.state->>'product','ChatGPT'),r.state->>'plan_name',days||' يوم',coalesce(r.state->>'funding_id'='debt',false),start_time,end_time,'active') returning * into rem;
       end if;
       update delivery_contracts set reminder_id=rem.id where id=c.id returning * into c;
     else
       update subscription_reminders set started_at=c.starts_at,expires_at=c.ends_at,status='active',feedback_status='none' where id=c.reminder_id;
     end if;
     insert into delivery_periods(owner_id,contract_id,request_id,funding_id,allocated,starts_at,ends_at,kind,snapshot)
       values(p_owner,c.id,r.id,funding,allocated,start_time,end_time,mode,coalesce(r.state->'target_before','{}'));
   end if;
   -- Compatibility links are mirrors; ownership and permission stay contract-based.
   update customer_totp_account_links set is_primary=false where customer_chat_id=r.customer_id;
   insert into customer_totp_account_links(customer_chat_id,account_id,is_primary,status) values(r.customer_id,a.legacy_id,true,'active')
     on conflict(customer_chat_id,account_id) do update set is_primary=true,status='active',linked_at=now();
   insert into totp_links(chat_id,account_id)
     select x.chat_id,x.account_id from jsonb_populate_record(null::totp_links,jsonb_build_object('chat_id',r.customer_id,'account_id',a.legacy_id)) x
     on conflict(chat_id) do update set account_id=excluded.account_id;
   outcome:=jsonb_build_object('contract_id',c.id,'reminder_id',c.reminder_id,'starts_at',c.starts_at,'ends_at',c.ends_at,'funding_id',funding);
   update delivery_requests set status='complete',result=outcome,revision=revision+1 where id=r.id returning * into r;
   insert into delivery_events(owner_id,request_id,kind,details) values(p_owner,r.id,'complete',outcome);
 end if;
 if p_action='undo' then
   if r.status<>'complete' then raise exception 'Only a completed delivery can be reversed';end if;
   select * into oldp from delivery_periods where request_id=r.id and not voided;
   if not found then raise exception 'No reversible subscription change';end if;
   if oldp.kind='adopt' then raise exception 'Legacy adoption keeps existing entitlement; manual relationship review required';end if;
   if exists(select 1 from delivery_periods where contract_id=oldp.contract_id and not voided and request_id<>r.id and recorded_at>oldp.recorded_at) then raise exception 'Undo later subscription changes first';end if;
   select * into c from delivery_contracts where id=oldp.contract_id for update;
   if oldp.kind='new' then
     if oldp.funding_id is not null then
       select * into op from finance_operations where owner_id=p_owner and id=oldp.funding_id;
       if op.kind='debt_issue' and exists(select 1 from finance_debts where owner_id=p_owner and source_operation_id=op.id and remaining<>original) then raise exception 'Debt has repayments; review money separately';end if;
     end if;
     update delivery_contracts set status='cancelled',revision=revision+1 where id=c.id;
     update subscription_reminders set status='cancelled' where id=c.reminder_id;
   else
     saved:=oldp.snapshot;
     update delivery_contracts set account_id=(saved->>'account_id')::uuid,starts_at=(saved->>'starts_at')::timestamptz,
       ends_at=(saved->>'ends_at')::timestamptz,revision=revision+1 where id=c.id returning * into c;
     update subscription_reminders set started_at=c.starts_at,expires_at=c.ends_at,status='active' where id=c.reminder_id;
   end if;
   update delivery_periods set voided=true where request_id=r.id;
   if not exists(select 1 from delivery_contracts where account_id=a.id and customer_id=r.customer_id and status='active' and ends_at>now()) then
     insert into delivery_legacy_holds(account_id,customer_id) values(a.id,r.customer_id)
       on conflict(account_id,customer_id) do update set reviewed=false;
   end if;
   update delivery_requests set result=coalesce(result,'{}')||'{"undone":true}'::jsonb where id=r.id returning * into r;
   insert into delivery_events(owner_id,request_id,kind,details) values(p_owner,r.id,'undo','{"credentials_exposed":true,"money_unchanged":true}');
 end if;
 select * into cfg from delivery_templates where owner_id=p_owner;
 return jsonb_build_object('managed',true,'duplicate',was_complete,'request',to_jsonb(r),'account',jsonb_build_object('id',a.id,'email',n.email,'code',a.code,'capacity',n.capacity,'enabled',a.enabled and n.is_active),
   'occupancy',delivery_occupancy(a.id),'template',to_jsonb(cfg),
   'contracts',coalesce((select jsonb_agg(to_jsonb(x)) from delivery_contracts x where x.owner_id=p_owner and x.customer_id=r.customer_id and x.status='active'),'[]'::jsonb),
   'legacy_reminders',coalesce((select jsonb_agg(jsonb_build_object('id',sr.id,'ends_at',sr.expires_at,'product',sr.product_name)) from subscription_reminders sr
     where sr.customer_chat_id=r.customer_id and sr.status='active' and sr.expires_at>now() and sr.product_name in ('ChatGPT','جات','شات جي بي تي')
       and not exists(select 1 from delivery_contracts where reminder_id=sr.id)),'[]'::jsonb),
   'funding',coalesce((select jsonb_agg(jsonb_build_object('id',f.id,'product',f.product,'sales',f.sales,'payload',f.payload,
     'available',f.sales-coalesce((select sum(dp.allocated) from delivery_periods dp where dp.owner_id=p_owner and dp.funding_id=f.id and not dp.voided),0)))
     from finance_operations f where owner_id=p_owner and customer_id=coalesce((r.state->>'payer_id')::bigint,r.customer_id) and kind in ('sale','debt_issue') and not historical
     and not exists(select 1 from finance_operations v where v.owner_id=p_owner and v.reference_id=f.id and v.kind in ('reversal','refund'))),'[]'::jsonb));
end; $$;

-- Secrets are returned only after a persisted sending claim, to server role.
create or replace function public.delivery_credentials(p_owner bigint,p_id uuid)
returns jsonb language sql stable set search_path=public as $$
 select jsonb_build_object('email',n.email,'password',n.password,'secret',n.totp_secret)
 from delivery_requests r join delivery_accounts a on a.id=r.account_id join chatgpt_shared_accounts n on n.id=a.native_id
 where r.owner_id=p_owner and r.id=p_id and r.status='sending' and a.enabled and n.is_active
   and md5(n.email||n.password||n.totp_secret)=r.state->>'credentials_hash';
$$;
create or replace function public.delivery_code_permission(p_customer bigint,p_legacy text)
returns jsonb language sql stable set search_path=public as $$
 select jsonb_build_object('managed',exists(select 1 from delivery_accounts a where (a.legacy_id=p_legacy or a.native_id::text=p_legacy) and
   (exists(select 1 from delivery_contracts where customer_id=p_customer and account_id=a.id)
    or exists(select 1 from delivery_periods dp join delivery_contracts c on c.id=dp.contract_id where c.customer_id=p_customer and dp.snapshot->>'account_id'=a.id::text))),
   'allowed',exists(select 1 from delivery_contracts c join delivery_accounts a on a.id=c.account_id join chatgpt_shared_accounts n on n.id=a.native_id
     join totp_accounts ta on ta.id::text=a.legacy_id and ta.secret=n.totp_secret
     where c.customer_id=p_customer and (a.legacy_id=p_legacy or a.native_id::text=p_legacy) and c.status='active' and c.starts_at<=now() and c.ends_at>now() and a.enabled and n.is_active));
$$;
create or replace function public.delivery_funding_managed(p_owner bigint,p_funding text)
returns boolean language sql stable set search_path=public as $$
 select exists(select 1 from delivery_periods where owner_id=p_owner and funding_id=p_funding)
    or exists(select 1 from delivery_requests where owner_id=p_owner and state->>'funding_id'=p_funding and status in ('reserved','sending','uncertain','sent'));
$$;
create or replace function public.delivery_reminder_managed(p_reminder bigint)
returns boolean language sql stable set search_path=public as $$
 select exists(select 1 from delivery_contracts where reminder_id=p_reminder);
$$;
create or replace function public.delivery_account_customers(p_account text)
returns jsonb language sql stable set search_path=public as $$
 select jsonb_build_object('managed',exists(select 1 from delivery_accounts where legacy_id=p_account or native_id::text=p_account),
   'resource',(select jsonb_build_object('capacity',n.capacity,'enabled',a.enabled and n.is_active,'occupancy',delivery_occupancy(a.id)) from delivery_accounts a join chatgpt_shared_accounts n on n.id=a.native_id where a.legacy_id=p_account or a.native_id::text=p_account),
   'customers',coalesce((select jsonb_agg(distinct q.customer_id) from (
     select c.customer_id from delivery_contracts c join delivery_accounts a on a.id=c.account_id
       where (a.legacy_id=p_account or a.native_id::text=p_account) and c.status='active' and c.starts_at<=now() and c.ends_at>now()
     union select l.customer_chat_id from customer_totp_account_links l join delivery_accounts a on a.legacy_id=l.account_id
       where (a.legacy_id=p_account or a.native_id::text=p_account) and l.status='active'
         and not exists(select 1 from delivery_contracts where account_id=a.id and customer_id=l.customer_chat_id)
         and exists(select 1 from subscription_reminders where customer_chat_id=l.customer_chat_id and status='active' and expires_at>now() and product_name in ('ChatGPT','جات','شات جي بي تي'))
   )q),'[]'::jsonb));
$$;
-- The financial ledger stays authoritative; an allocated sale cannot be
-- silently reversed/refunded while its subscription remains in service.
create or replace function public.delivery_financial_guard() returns trigger language plpgsql set search_path=public as $$
begin
 if new.kind in ('reversal','refund') and (exists(select 1 from delivery_periods where owner_id=new.owner_id and funding_id=new.reference_id and not voided)
   or exists(select 1 from delivery_requests where owner_id=new.owner_id and state->>'funding_id'=new.reference_id and status in ('reserved','sending','uncertain','sent') and (status<>'reserved' or hold_until>now()))) then
   raise exception 'Delivery allocation exists; review subscription before money correction';
 end if;
 return new;
end; $$;
drop trigger if exists delivery_financial_guard on finance_operations;
create trigger delivery_financial_guard before insert on finance_operations for each row execute function delivery_financial_guard();
create or replace function public.delivery_assignment_guard() returns trigger language plpgsql set search_path=public as $$
begin
 if new.status='active' and exists(select 1 from delivery_accounts where native_id=new.account_id) and
    not exists(select 1 from delivery_contracts c join delivery_accounts a on a.id=c.account_id where a.native_id=new.account_id and c.customer_id=new.customer_chat_id and c.status='active' and c.ends_at>now()) then
   raise exception 'Managed account requires explicit /link subscription delivery';
 end if;
 return new;
end; $$;
drop trigger if exists delivery_assignment_guard on chatgpt_account_assignments;
create trigger delivery_assignment_guard before insert or update on chatgpt_account_assignments for each row execute function delivery_assignment_guard();
do $$ declare t text; f regprocedure;begin
 foreach t in array array['delivery_accounts','delivery_templates','delivery_legacy_holds','delivery_contracts','delivery_requests','delivery_periods','delivery_events'] loop
   execute format('alter table %I enable row level security',t);
   execute format('revoke all on %I from anon,authenticated',t);
   execute format('grant all on %I to service_role',t);
 end loop;
 grant usage,select on sequence delivery_events_id_seq to service_role;
 for f in select oid::regprocedure from pg_proc where pronamespace='public'::regnamespace and proname in ('delivery_manage','delivery_occupancy','delivery_credentials','delivery_code_permission','delivery_funding_managed','delivery_reminder_managed','delivery_account_customers','delivery_financial_guard','delivery_assignment_guard') loop
   execute format('revoke all on function %s from public,anon,authenticated',f);
   execute format('grant execute on function %s to service_role',f);
 end loop;
end;$$;
commit;
