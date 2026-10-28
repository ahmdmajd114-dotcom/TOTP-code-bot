-- MedBox financial ledger. Run once in the project's Supabase SQL Editor.
-- Installing this schema DOES NOT switch existing accounting. The owner reviews
-- and activates a staged snapshot from the bot. No old Sheet cells are deleted.
begin;

create table if not exists public.finance_settings (
  owner_id bigint primary key,
  mode text not null default 'legacy' check (mode in ('legacy','review','active')),
  snapshot jsonb, cutover_at timestamptz, updated_at timestamptz not null default now()
);
create table if not exists public.finance_operations (
  owner_id bigint not null, id text not null, payload jsonb not null,
  kind text not null, occurred_at timestamptz, recorded_at timestamptz not null default now(),
  customer_id bigint, product text, cash_in bigint not null default 0,
  cash_out bigint not null default 0, sales bigint, expense bigint not null default 0,
  refunded bigint not null default 0, reference_id text,
  historical boolean not null default false,
  primary key(owner_id,id), check(cash_in>=0 and cash_out>=0 and expense>=0 and refunded>=0)
);
create table if not exists public.finance_accounts (
  owner_id bigint not null, key text not null, balance bigint not null default 0,
  primary key(owner_id,key)
);
create table if not exists public.finance_entries (
  owner_id bigint not null, operation_id text not null, account_key text not null,
  delta bigint not null, primary key(owner_id,operation_id,account_key),
  foreign key(owner_id,operation_id) references public.finance_operations(owner_id,id)
);
create table if not exists public.finance_debts (
  owner_id bigint not null, id text not null, customer_id bigint not null,
  customer_name text not null default '', product text not null,
  product_id text, plan_id text, plan_name text,
  original bigint not null check(original>0), remaining bigint not null check(remaining>=0),
  created_at timestamptz not null default now(), last_paid_at timestamptz,
  last_operation_id text, source_operation_id text, legacy_row integer,
  primary key(owner_id,id), check(remaining<=original)
);
create table if not exists public.finance_outbox (
  owner_id bigint not null, operation_id text not null,
  status text not null default 'pending' check(status in ('pending','working','done')),
  attempts integer not null default 0, next_at timestamptz not null default now(),
  locked_until timestamptz, last_error text,
  primary key(owner_id,operation_id),
  foreign key(owner_id,operation_id) references public.finance_operations(owner_id,id)
);
create table if not exists public.finance_drafts (
  owner_id bigint not null, id text not null, state jsonb not null,
  updated_at timestamptz not null default now(), primary key(owner_id,id)
);
create index if not exists finance_operations_date on public.finance_operations(owner_id,occurred_at);
create index if not exists finance_operations_customer on public.finance_operations(owner_id,customer_id,recorded_at);
create index if not exists finance_outbox_due on public.finance_outbox(owner_id,next_at) where status<>'done';

-- Private helper used only inside the transaction; no direct client privilege.
create or replace function public.finance_delta(p_owner bigint,p_id text,p_key text,p_delta bigint)
returns void language plpgsql set search_path=public as $$
begin
  if p_delta=0 then return; end if;
  insert into finance_accounts(owner_id,key,balance) values(p_owner,p_key,p_delta)
    on conflict(owner_id,key) do update set balance=finance_accounts.balance+excluded.balance;
  insert into finance_entries(owner_id,operation_id,account_key,delta) values(p_owner,p_id,p_key,p_delta)
    on conflict(owner_id,operation_id,account_key) do update set delta=finance_entries.delta+excluded.delta;
end; $$;

create or replace function public.finance_post(p_owner bigint,p jsonb)
returns jsonb language plpgsql set search_path=public as $$
declare
  k text:=p->>'kind'; oid text:=p->>'id'; old jsonb; x jsonb; d finance_debts%rowtype;
  ref finance_operations%rowtype; cid bigint; total bigint:=0; sale bigint:=0;
  used bigint:=0; added bigint:=0; owed bigint:=0; repaid bigint:=0; fee bigint:=0;
  amount bigint; cin bigint:=0; cout bigint:=0; exp bigint:=0; refund bigint:=0;
  stamp timestamptz; wallet text; bal bigint; refid text:=p->>'reference_id';
begin
  perform pg_advisory_xact_lock(p_owner);
  select payload into old from finance_operations where owner_id=p_owner and id=oid;
  if found then
    if old<>p then raise exception 'Operation ID reused with different contents'; end if;
    return jsonb_build_object('id',oid,'duplicate',true);
  end if;
  if coalesce((select mode from finance_settings where owner_id=p_owner),'legacy')<>'active' then
    raise exception 'Financial ledger is not active';
  end if;
  if oid is null or length(oid)<8 or k not in ('sale','debt_repayment','credit_deposit','expense','refund','transfer','capital','withdrawal','debt_issue','credit_adjustment','vault_adjustment','reversal') then
    raise exception 'Invalid financial operation';
  end if;
  if coalesce(p->>'proof_id','')<>'' and exists(select 1 from finance_operations where owner_id=p_owner and payload->>'proof_id'=p->>'proof_id') then
    raise exception 'Payment proof already used';
  end if;
  stamp:=(p->>'occurred_at')::timestamptz;
  if stamp is null or stamp>now()+interval '1 minute' then raise exception 'Invalid transaction date'; end if;
  cid:=(p->>'customer_id')::bigint;
  -- jsonb numeric -> bigint can round decimal values: reject them explicitly.
  foreach wallet in array array['sales','credit_used','credit_added','new_debt','fee'] loop
    if p ? wallet and coalesce(p->>wallet,'')!~'^[0-9]+$' then raise exception 'Amounts must be integer dinars'; end if;
  end loop;
  sale:=coalesce((p->>'sales')::bigint,0); used:=coalesce((p->>'credit_used')::bigint,0);
  added:=coalesce((p->>'credit_added')::bigint,0); owed:=coalesce((p->>'new_debt')::bigint,0);
  fee:=coalesce((p->>'fee')::bigint,0);
  for x in select value from jsonb_array_elements(coalesce(p->'payments','[]')) loop
    if coalesce(x->>'amount','')!~'^[0-9]+$' or (x->>'amount')::bigint<=0 or coalesce(x->>'wallet','')='' then
      raise exception 'Invalid wallet payment';
    end if;
    total:=total+(x->>'amount')::bigint;
  end loop;
  if k='reversal' then
    select * into ref from finance_operations where owner_id=p_owner and id=refid;
    if not found or ref.historical or ref.kind not in ('sale','expense','capital','withdrawal','transfer','credit_deposit','debt_repayment','credit_adjustment','vault_adjustment') then
      raise exception 'This operation requires a linked adjustment instead';
    end if;
    if exists(select 1 from finance_operations where owner_id=p_owner and reference_id=refid and kind in ('refund','reversal')) then
      raise exception 'Operation already has a refund or reversal';
    end if;
    if coalesce((ref.payload->>'new_debt')::bigint,0)>0 then
      select * into d from finance_debts where owner_id=p_owner and source_operation_id=refid;
      if d.remaining<>d.original then raise exception 'Debt has repayments; reverse them first'; end if;
    end if;
    if exists(select 1 from finance_entries e join finance_accounts a on a.owner_id=e.owner_id and a.key=e.account_key
      where e.owner_id=p_owner and e.operation_id=refid and e.account_key like 'credit:%' and a.balance-e.delta<0) then
      raise exception 'Customer credit has been used';
    end if;
    insert into finance_operations(owner_id,id,payload,kind,occurred_at,customer_id,product,cash_in,cash_out,sales,expense,refunded,reference_id)
      values(p_owner,oid,p,k,stamp,ref.customer_id,ref.product,0,0,0,0,0,refid);
    for x in select to_jsonb(e) from finance_entries e where owner_id=p_owner and operation_id=refid loop
      perform finance_delta(p_owner,oid,x->>'account_key',-(x->>'delta')::bigint);
    end loop;
    for x in select value from jsonb_array_elements(coalesce(ref.payload->'settlements','[]')) loop
      update finance_debts set remaining=remaining+(x->>'amount')::bigint,last_operation_id=oid
        where owner_id=p_owner and id=x->>'debt_id';
    end loop;
    update finance_debts set remaining=0,last_operation_id=oid where owner_id=p_owner and source_operation_id=refid;
    insert into finance_outbox(owner_id,operation_id) values(p_owner,oid);
    return jsonb_build_object('id',oid,'duplicate',false);
  end if;
  for x in select value from jsonb_array_elements(coalesce(p->'settlements','[]')) loop
    if coalesce(x->>'amount','')!~'^[0-9]+$' or (x->>'amount')::bigint<=0 then raise exception 'Invalid settlement'; end if;
    amount:=(x->>'amount')::bigint;
    select * into d from finance_debts where owner_id=p_owner and id=x->>'debt_id' for update;
    if not found or d.customer_id is distinct from cid or amount>d.remaining or (x ? 'expected_remaining' and (x->>'expected_remaining')::bigint<>d.remaining) then raise exception 'Debt balance changed'; end if;
    repaid:=repaid+amount;
  end loop;
  if (select count(*) from jsonb_array_elements(coalesce(p->'settlements','[]')))<>(select count(distinct value->>'debt_id') from jsonb_array_elements(coalesce(p->'settlements','[]'))) then
    raise exception 'Duplicate debt allocation';
  end if;
  if k in ('sale','debt_repayment','credit_deposit','debt_issue') then
    if total+used+owed+fee<>sale+repaid+added or total+used+owed+fee=0 then raise exception 'Payment allocation does not balance'; end if;
    if k='debt_repayment' and (repaid=0 or sale<>0 or owed<>0 or fee<>0) then raise exception 'Invalid repayment'; end if;
    if k='credit_deposit' and (sale<>0 or repaid<>0 or owed<>0 or fee<>0 or used<>0 or added<>total) then raise exception 'Invalid credit deposit'; end if;
    if k='debt_issue' and (total<>0 or sale<>owed or repaid<>0 or used<>0 or added<>0 or fee<>0) then raise exception 'Invalid debt'; end if;
    if k='sale' and (sale<=0 or coalesce(p->>'product','')='') then raise exception 'Sale requires product and amount'; end if;
    cin:=total; exp:=fee;
  else
    if sale+used+added+owed+repaid+fee<>0 then raise exception 'Unexpected allocation'; end if;
    if k in ('credit_adjustment','vault_adjustment') and total<>0 then raise exception 'Adjustments cannot include cash payments'; end if;
    if total=0 and k not in ('credit_adjustment','vault_adjustment') then raise exception 'Missing amount'; end if;
    if k in ('expense','refund','withdrawal') then cout:=total; end if;
    if k='capital' then cin:=total; end if;
    if k='expense' then exp:=total; end if;
  end if;
  if used+added+owed+repaid>0 and cid is null then raise exception 'Customer required'; end if;
  if used>0 then
    select balance into bal from finance_accounts where owner_id=p_owner and key='credit:'||cid;
    if coalesce(bal,0)<used then raise exception 'Insufficient customer credit'; end if;
  end if;
  if k='refund' then
    select * into ref from finance_operations where owner_id=p_owner and id=refid;
    if not found or ref.kind<>'sale' or ref.customer_id is distinct from cid or ref.historical then raise exception 'Select the original sale'; end if;
    if exists(select 1 from finance_operations where owner_id=p_owner and reference_id=refid and kind='reversal') then raise exception 'Sale was reversed'; end if;
    if total+coalesce((select sum(refunded) from finance_operations where owner_id=p_owner and reference_id=refid),0)>ref.sales-coalesce((select sum(remaining) from finance_debts where owner_id=p_owner and source_operation_id=refid),0) then raise exception 'Refund exceeds collected sale'; end if;
    refund:=total;
  end if;
  if k='transfer' then
    if jsonb_array_length(p->'payments')<>1 or coalesce(p->>'to_wallet','')='' or p->>'to_wallet'=p->'payments'->0->>'wallet' then raise exception 'Choose two different wallets'; end if;
  end if;
  insert into finance_operations(owner_id,id,payload,kind,occurred_at,customer_id,product,cash_in,cash_out,sales,expense,refunded,reference_id)
    values(p_owner,oid,p,k,stamp,cid,p->>'product',cin,cout,sale,exp,refund,refid);
  for x in select value from jsonb_array_elements(coalesce(p->'payments','[]')) loop
    amount:=(x->>'amount')::bigint; wallet:=x->>'wallet';
    perform finance_delta(p_owner,oid,'wallet:'||wallet,case when k in ('expense','refund','withdrawal','transfer') then -amount else amount end);
    if k='transfer' then perform finance_delta(p_owner,oid,'wallet:'||(p->>'to_wallet'),amount); end if;
  end loop;
  perform finance_delta(p_owner,oid,'credit:'||cid,added-used);
  if k in ('credit_adjustment','vault_adjustment') then
    if coalesce(p->>'adjustment','')!~'^-?[0-9]+$' or coalesce(p->>'description','')='' then raise exception 'Adjustment requires amount and reason'; end if;
    if k='credit_adjustment' and cid is null then raise exception 'Customer required'; end if;
    wallet:=case when k='credit_adjustment' then 'credit:'||cid else 'wallet:'||(p->>'wallet') end;
    if wallet is null or wallet='wallet:' then raise exception 'Missing wallet'; end if;
    if k='vault_adjustment' and p ? 'expected_balance' and coalesce((select balance from finance_accounts where owner_id=p_owner and key=wallet),0)<>(p->>'expected_balance')::bigint then raise exception 'Wallet balance changed'; end if;
    if k='credit_adjustment' and coalesce((select balance from finance_accounts where owner_id=p_owner and key=wallet),0)+(p->>'adjustment')::bigint<0 then raise exception 'Credit cannot be negative'; end if;
    perform finance_delta(p_owner,oid,wallet,(p->>'adjustment')::bigint);
  end if;
  for x in select value from jsonb_array_elements(coalesce(p->'settlements','[]')) loop
    update finance_debts set remaining=remaining-(x->>'amount')::bigint,last_paid_at=stamp,last_operation_id=oid
      where owner_id=p_owner and id=x->>'debt_id';
  end loop;
  if owed>0 then
    insert into finance_debts(owner_id,id,customer_id,customer_name,product,product_id,plan_id,plan_name,original,remaining,created_at,source_operation_id,last_operation_id)
      values(p_owner,'DEBT-'||oid,cid,coalesce(p->>'customer_name',''),coalesce(p->>'product',''),p->>'product_id',p->>'plan_id',p->>'plan_name',owed,owed,stamp,oid,oid);
  end if;
  insert into finance_outbox(owner_id,operation_id) values(p_owner,oid);
  return jsonb_build_object('id',oid,'duplicate',false);
end; $$;

create or replace function public.finance_correct(p_owner bigint,p_reference text,p_replacement jsonb)
returns jsonb language plpgsql set search_path=public as $$
declare r jsonb;
begin
  perform pg_advisory_xact_lock(p_owner);
  r:=finance_post(p_owner,jsonb_build_object('id','REV-'||(p_replacement->>'id'),'kind','reversal','reference_id',p_reference,'occurred_at',p_replacement->>'occurred_at'));
  return finance_post(p_owner,p_replacement);
end; $$;

do $$ begin
  if to_regclass('public.subscription_reminders') is not null then
    alter table public.subscription_reminders add column if not exists financial_operation_id text;
    create unique index if not exists subscription_financial_operation_unique on public.subscription_reminders(financial_operation_id) where financial_operation_id is not null;
  end if;
end; $$;

create or replace function public.finance_setup(p_owner bigint,p_action text,p_snapshot jsonb default null)
returns jsonb language plpgsql set search_path=public as $$
declare s finance_settings%rowtype; x jsonb; oid text;
begin
  perform pg_advisory_xact_lock(p_owner);
  insert into finance_settings(owner_id) values(p_owner) on conflict do nothing;
  select * into s from finance_settings where owner_id=p_owner for update;
  if p_action='status' then return jsonb_build_object('mode',s.mode,'cutover_at',s.cutover_at); end if;
  if s.mode='active' then raise exception 'Ledger already active'; end if;
  if p_action='begin' then
    update finance_settings set mode='review',snapshot=null,updated_at=now() where owner_id=p_owner;
  elsif p_action='stage' and s.mode='review' then
    if p_snapshot is null then raise exception 'Missing snapshot'; end if;
    update finance_settings set snapshot=p_snapshot,updated_at=now() where owner_id=p_owner;
  elsif p_action='cancel' then
    update finance_settings set mode='legacy',snapshot=null,updated_at=now() where owner_id=p_owner;
  elsif p_action='activate' and s.mode='review' and s.snapshot is not null then
    if exists(select 1 from finance_operations where owner_id=p_owner) then raise exception 'Ledger is not empty'; end if;
    for x in select value from jsonb_array_elements(s.snapshot->'accounts') loop
      insert into finance_accounts(owner_id,key,balance) values(p_owner,x->>'key',(x->>'balance')::bigint);
    end loop;
    for x in select value from jsonb_array_elements(s.snapshot->'debts') loop
      insert into finance_debts(owner_id,id,customer_id,customer_name,product,product_id,plan_id,plan_name,original,remaining,created_at,last_paid_at,legacy_row)
      values(p_owner,x->>'id',(x->>'customer_id')::bigint,coalesce(x->>'customer_name',''),x->>'product',x->>'product_id',x->>'plan_id',x->>'plan_name',(x->>'original')::bigint,(x->>'remaining')::bigint,coalesce((x->>'created_at')::timestamptz,now()),(x->>'last_paid_at')::timestamptz,(x->>'legacy_row')::integer);
    end loop;
    for x in select value from jsonb_array_elements(s.snapshot->'operations') loop
      insert into finance_operations(owner_id,id,payload,kind,occurred_at,customer_id,product,cash_in,cash_out,sales,expense,refunded,historical)
      values(p_owner,x->>'id',x,x->>'kind',(x->>'occurred_at')::timestamptz,(x->>'customer_id')::bigint,x->>'product',coalesce((x->>'cash_in')::bigint,0),coalesce((x->>'cash_out')::bigint,0),null,coalesce((x->>'expense')::bigint,0),0,true);
    end loop;
    insert into finance_operations(owner_id,id,payload,kind,occurred_at,historical,sales)
      values(p_owner,'OPEN-'||p_owner,jsonb_build_object('kind','opening','accounts',s.snapshot->'accounts','warnings',s.snapshot->'warnings'),'opening',now(),true,0);
    update finance_settings set mode='active',cutover_at=now(),snapshot=null,updated_at=now() where owner_id=p_owner;
  else raise exception 'Invalid setup transition'; end if;
  return jsonb_build_object('mode',(select mode from finance_settings where owner_id=p_owner));
end; $$;

-- One MVCC snapshot for reports: concurrent sales cannot move rows between
-- pagination requests or give the customer card mismatched debt/credit totals.
create or replace function public.finance_read(p_owner bigint)
returns jsonb language sql stable set search_path=public as $$
 select jsonb_build_object(
   'operations',coalesce((select jsonb_agg(to_jsonb(o) order by recorded_at,id) from finance_operations o where owner_id=p_owner),'[]'::jsonb),
   'debts',coalesce((select jsonb_agg(to_jsonb(d) order by id) from finance_debts d where owner_id=p_owner),'[]'::jsonb),
   'accounts',coalesce((select jsonb_object_agg(key,balance) from finance_accounts where owner_id=p_owner),'{}'::jsonb)
 );
$$;

drop function if exists public.finance_claim_outbox(bigint);
create or replace function public.finance_claim_outbox(p_owner bigint,p_id text default null)
returns jsonb language plpgsql set search_path=public as $$
declare oid text; token integer;
begin
  select operation_id into oid from finance_outbox where owner_id=p_owner and status<>'done' and next_at<=now() and (p_id is null or operation_id=p_id)
    and (locked_until is null or locked_until<now()) order by next_at,operation_id for update skip locked limit 1;
  if oid is null then return null; end if;
  update finance_outbox set status='working',attempts=attempts+1,locked_until=now()+interval '5 minutes'
    where owner_id=p_owner and operation_id=oid returning attempts into token;
  return jsonb_build_object('id',oid,'token',token,'payload',(select payload from finance_operations where owner_id=p_owner and id=oid));
end; $$;

create or replace function public.finance_ack_outbox(p_owner bigint,p_id text,p_token integer,p_error text default null)
returns void language plpgsql set search_path=public as $$
begin
  update finance_outbox set status=case when p_error is null then 'done' else 'pending' end,
    locked_until=null,last_error=left(p_error,500),next_at=now()+interval '1 minute'*least(60,greatest(1,attempts))
    where owner_id=p_owner and operation_id=p_id and attempts=p_token and status='working';
end; $$;

-- Service role only: these RPCs accept an explicit owner ID supplied by the bot.
do $$ declare t text; f regprocedure; begin
  foreach t in array array['finance_settings','finance_operations','finance_accounts','finance_entries','finance_debts','finance_outbox','finance_drafts'] loop
    execute format('alter table public.%I enable row level security',t);
    execute format('revoke all on public.%I from anon, authenticated',t);
    execute format('grant all on public.%I to service_role',t);
  end loop;
  for f in select oid::regprocedure from pg_proc where pronamespace='public'::regnamespace and proname like 'finance_%' loop
    execute format('revoke all on function %s from public, anon, authenticated',f);
    execute format('grant execute on function %s to service_role',f);
  end loop;
end; $$;
commit;
