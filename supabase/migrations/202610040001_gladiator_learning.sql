-- Apply once using the Supabase SQL editor or your authorized migration tool.
-- Application API keys are not schema-management credentials.
begin;
create table public.gladiator_learning_strategies (
  catalog_version text not null,
  strategy_id text not null,
  definition jsonb not null,
  primary key (catalog_version, strategy_id)
);
create table public.gladiator_learning_matches (
  id text primary key,
  scope text not null,
  catalog_version text not null,
  controller_version text not null,
  payload jsonb not null,
  status text not null default 'selected' check (status in ('selected','scored','skipped')),
  rewards jsonb,
  result jsonb,
  created_at timestamptz not null default now(),
  finished_at timestamptz
);
create index on public.gladiator_learning_matches (scope, finished_at desc) where status = 'scored';
create table public.gladiator_learning_decisions (
  match_id text references public.gladiator_learning_matches(id) on delete cascade,
  player_id text not null,
  strategy_id text not null,
  context jsonb not null,
  distribution jsonb not null,
  selection_probability double precision not null check (selection_probability > 0 and selection_probability <= 1),
  reward double precision check (reward >= 0 and reward <= 1),
  primary key (match_id, player_id)
);
create table public.gladiator_learning_checkpoints (
  id bigint generated always as identity primary key,
  match_id text not null unique references public.gladiator_learning_matches(id),
  scope text not null,
  state jsonb not null,
  created_at timestamptz not null default now()
);
alter table public.gladiator_learning_strategies enable row level security;
alter table public.gladiator_learning_matches enable row level security;
alter table public.gladiator_learning_decisions enable row level security;
alter table public.gladiator_learning_checkpoints enable row level security;
revoke all on public.gladiator_learning_strategies, public.gladiator_learning_matches,
  public.gladiator_learning_decisions, public.gladiator_learning_checkpoints from anon, authenticated;
grant all on public.gladiator_learning_strategies, public.gladiator_learning_matches,
  public.gladiator_learning_decisions, public.gladiator_learning_checkpoints to service_role;
grant usage, select on sequence public.gladiator_learning_checkpoints_id_seq to service_role;

create function public.gladiator_learning_begin(record jsonb, catalog jsonb)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare d jsonb; s record; existing jsonb;
begin
  if jsonb_typeof(record->'payload'->'decisions') <> 'array' or
     jsonb_array_length(record->'payload'->'decisions') < 2 then
    raise exception 'Invalid learning decision set';
  end if;
  for s in select key, value from jsonb_each(catalog) loop
    insert into public.gladiator_learning_strategies values (record->>'catalog_version', s.key, s.value)
      on conflict do nothing;
    if (select definition from public.gladiator_learning_strategies
        where catalog_version = record->>'catalog_version' and strategy_id = s.key) <> s.value then
      raise exception 'Catalog versions are immutable';
    end if;
  end loop;
  insert into public.gladiator_learning_matches(id,scope,catalog_version,controller_version,payload)
    values(record->>'id',record->>'scope',record->>'catalog_version',record->>'controller_version',record->'payload')
    on conflict do nothing;
  select payload into existing from public.gladiator_learning_matches where id = record->>'id' for update;
  if existing <> record->'payload' then raise exception 'Conflicting match decisions'; end if;
  for d in select value from jsonb_array_elements(record->'payload'->'decisions') loop
    if not (catalog ? (d->>'strategy_id')) then raise exception 'Unknown strategy'; end if;
    insert into public.gladiator_learning_decisions(match_id,player_id,strategy_id,context,distribution,selection_probability)
      values(record->>'id',d->>'player_id',d->>'strategy_id',d->'context',d->'distribution',
             (d->>'selection_probability')::double precision) on conflict do nothing;
  end loop;
  return jsonb_build_object('status','selected');
end $$;

create function public.gladiator_learning_finish(p_result jsonb)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare m public.gladiator_learning_matches; checkpoint bigint; recent jsonb; sources jsonb;
        shared jsonb; specific jsonb; valid boolean;
begin
  select * into m from public.gladiator_learning_matches where id = p_result->>'id' for update;
  if not found then raise exception 'Learning match not registered'; end if;
  if m.status <> 'selected' then
    select id into checkpoint from public.gladiator_learning_checkpoints where match_id = m.id;
    return jsonb_build_object('status',m.status,'checkpoint_id',checkpoint);
  end if;
  valid := p_result->>'reason' is null;
  if valid then
    if jsonb_typeof(p_result->'rewards') <> 'object' or
       (select count(*) from jsonb_object_keys(p_result->'rewards')) <>
       (select count(*) from public.gladiator_learning_decisions where match_id = m.id) or
       exists(select 1 from public.gladiator_learning_decisions where match_id = m.id
              and not ((p_result->'rewards') ? player_id)) then
      raise exception 'Reward roster does not match decisions';
    end if;
    update public.gladiator_learning_decisions set reward = (p_result->'rewards'->>player_id)::double precision
      where match_id = m.id;
    if exists(select 1 from public.gladiator_learning_decisions where match_id = m.id and reward is null) then
      raise exception 'Missing reward';
    end if;
  end if;
  update public.gladiator_learning_matches set status = case when valid then 'scored' else 'skipped' end,
    rewards = p_result->'rewards', result = p_result, finished_at = clock_timestamp()
    where id = m.id;
  if valid then
    select jsonb_agg(to_jsonb(r) order by r.finished_at desc,r.id desc),
           jsonb_agg(r.id order by r.finished_at desc,r.id desc) into recent,sources from (
      select id,payload,rewards,finished_at from public.gladiator_learning_matches
      where scope = m.scope and status = 'scored' order by finished_at desc,id desc limit 200
    ) r;
    -- Store derived sufficient statistics plus source rows for independent replay.
    with observations as (
      select d->>'strategy_id' as strategy, (r->'rewards'->>(d->>'player_id'))::double precision as reward
      from jsonb_array_elements(recent) r, jsonb_array_elements(r->'payload'->'decisions') d
    ), totals as (select strategy,count(*) as n,sum(reward) as total from observations group by strategy)
    select jsonb_object_agg(strategy,jsonb_build_object('count',n,'total',total)) into shared from totals;
    with observations as (
      select d->'context' as context,d->>'strategy_id' as strategy,
             (r->'rewards'->>(d->>'player_id'))::double precision as reward
      from jsonb_array_elements(recent) r, jsonb_array_elements(r->'payload'->'decisions') d
    ), totals as (select context,strategy,count(*) as n,sum(reward) as total from observations group by context,strategy)
    select jsonb_agg(jsonb_build_object('context',context,'strategy_id',strategy,'count',n,'total',total))
      into specific from totals;
    insert into public.gladiator_learning_checkpoints(match_id,scope,state)
      values(m.id,m.scope,jsonb_build_object('algorithm','epsilon-greedy-context-v1','epsilon',0.2,
        'prior_count',5,'catalog_version',m.catalog_version,'source_matches',sources,
        'pooled',shared,'contexts',specific,'source_rows',recent)) returning id into checkpoint;
  end if;
  return jsonb_build_object('status',case when valid then 'scored' else 'skipped' end,'checkpoint_id',checkpoint);
end $$;
revoke all on function public.gladiator_learning_begin(jsonb,jsonb), public.gladiator_learning_finish(jsonb) from public, anon, authenticated;
grant execute on function public.gladiator_learning_begin(jsonb,jsonb), public.gladiator_learning_finish(jsonb) to service_role;
commit;
