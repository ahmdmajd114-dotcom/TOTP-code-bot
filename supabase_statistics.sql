-- Read-only statistics extension. Does not import, activate or modify money.
-- Run in the same Supabase project, AFTER supabase_financial_ledger.sql.
begin;
create or replace function public.finance_statistics_read(p_owner bigint)
returns jsonb language plpgsql stable set search_path=public as $$
declare result jsonb; data jsonb; statuses jsonb:='{}'; sources jsonb:='{}'; spec jsonb; fields text[]; name text; predicate text;
begin
  if coalesce((select mode from finance_settings where owner_id=p_owner),'legacy')<>'active' then
    raise exception 'Financial ledger is not active';
  end if;
  result:=finance_read(p_owner)||jsonb_build_object('as_of',statement_timestamp(),'statistics_extension',true,
    'cutover_at',(select cutover_at from finance_settings where owner_id=p_owner),
    'outbox',coalesce((select jsonb_agg(jsonb_build_object('operation_id',operation_id,'status',status,'attempts',attempts,'last_error',last_error)) from finance_outbox where owner_id=p_owner and status<>'done'),'[]'::jsonb));
  -- Explicit column allowlists: passwords, TOTP secrets, Business IDs, message
  -- bodies and authentication material never enter the reporting snapshot.
  for spec in select value from jsonb_array_elements('[
    {"table":"catalog_products","fields":["id","name","aliases","is_active"]},
    {"table":"subscription_reminders","fields":["id","customer_chat_id","subscription_type","product_name","plan_name","started_at","expires_at","status","feedback_status","source","instagram_account","financial_operation_id"]},
    {"table":"chatgpt_shared_accounts","fields":["id","email","capacity","is_active"]},
    {"table":"totp_accounts","fields":["id","label","is_private"]},
    {"table":"customer_totp_account_links","fields":["customer_chat_id","account_id","status","linked_at"]},
    {"table":"chatgpt_account_assignments","fields":["id","customer_chat_id","account_id","status","assigned_at"]},
    {"table":"continuity_bot_contacts","fields":["customer_chat_id","started_at"]},
    {"table":"continuity_bot_invites","fields":["customer_chat_id","status","sent_at","started_at"]},
    {"table":"customer_campaigns","fields":["id","status","created_at"],"owner":true},
    {"table":"customer_campaign_recipients","fields":["id","campaign_id","customer_chat_id","delivery_status","delivered_at"],"campaign_owner":true}
  ]'::jsonb) loop
    name:=spec->>'table';
    if to_regclass('public.'||name) is null then
      statuses:=statuses||jsonb_build_object(name,'المصدر غير مثبت');continue;
    end if;
    select array_agg(value) into fields from jsonb_array_elements_text(spec->'fields');
    predicate:='';
    if spec->>'owner'='true' then predicate:=' where t.owner_user_id=$2'; end if;
    if spec->>'campaign_owner'='true' then
      if to_regclass('public.customer_campaigns') is null then
        statuses:=statuses||jsonb_build_object(name,'جدول الحملات غير مثبت');continue;
      end if;
      predicate:=' where t.campaign_id in (select id from customer_campaigns where owner_user_id=$2)';
    end if;
    if name='totp_accounts' then
      execute 'select coalesce(jsonb_agg(jsonb_build_object(''id'',id,''label'',label,''is_private'',coalesce(to_jsonb(t)->>''link_code'','''') like ''private_%'')),''[]''::jsonb) from public.totp_accounts t' into data;
    else
      execute format('select coalesce(jsonb_agg((select jsonb_object_agg(k,to_jsonb(t)->k) from unnest($1) k)),''[]''::jsonb) from public.%I t',name)||predicate
      into data using fields,p_owner;
    end if;
    sources:=sources||jsonb_build_object(name,data);statuses:=statuses||jsonb_build_object(name,'ok');
  end loop;
  return result||jsonb_build_object('operational',sources,'source_status',statuses);
end; $$;
revoke all on function public.finance_statistics_read(bigint) from public,anon,authenticated;
grant execute on function public.finance_statistics_read(bigint) to service_role;
commit;
