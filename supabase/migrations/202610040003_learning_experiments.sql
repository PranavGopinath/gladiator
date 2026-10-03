begin;
create table public.learning_experiments (
  id uuid primary key,
  revision bigint not null check (revision > 0),
  payload jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create table public.learning_experiment_runs (
  match_id text primary key,
  experiment_id uuid not null references public.learning_experiments(id),
  payload jsonb not null,
  created_at timestamptz not null default now()
);
create index on public.learning_experiment_runs(experiment_id, created_at);
alter table public.learning_experiments enable row level security;
alter table public.learning_experiment_runs enable row level security;
revoke all on public.learning_experiments, public.learning_experiment_runs from anon, authenticated;
grant all on public.learning_experiments, public.learning_experiment_runs to service_role;

-- Old decisions have no learnable field and remain eligible. Fixed opponents
-- in new experiments are excluded from both pooled and contextual statistics.
do $$
declare definition text;
begin
  definition := pg_get_functiondef('public.learning_finish(jsonb)'::regprocedure);
  definition := replace(definition,
    'jsonb_array_elements(r->''payload''->''decisions'') d',
    'jsonb_array_elements(r->''payload''->''decisions'') d where coalesce((d->>''learnable'')::boolean, true)');
  execute definition;
end $$;

create function public.learning_checkpoint_catalog()
returns jsonb language sql security invoker set search_path = '' as $$
  select coalesce(jsonb_agg(jsonb_build_object(
    'id',c.id, 'match_id',c.match_id, 'created_at',c.created_at,
    'algorithm',c.state->>'algorithm', 'catalog_version',c.state->>'catalog_version',
    'contexts',(select coalesce(jsonb_agg(distinct d->'context'),'[]'::jsonb)
      from jsonb_array_elements(c.state->'source_rows') r,
           jsonb_array_elements(r->'payload'->'decisions') d
      where coalesce((d->>'learnable')::boolean,true)),
    'objectives',(select coalesce(jsonb_agg(distinct r->'payload'->'configuration'->'objective_hash'),'[]'::jsonb)
      from jsonb_array_elements(c.state->'source_rows') r)
  ) order by c.id desc),'[]'::jsonb)
  from (select * from public.learning_checkpoints order by id desc limit 100) c;
$$;

create function public.learning_experiment_commit(
  p_id uuid, p_expected_revision bigint, p_payload jsonb,
  p_run jsonb default null, p_registration jsonb default null,
  p_result jsonb default null, p_checkpoint jsonb default null)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare existing public.learning_experiments; run_before jsonb; result jsonb; catalog jsonb;
begin
  -- One deployment operates one experiment at a time. Serialize creation and
  -- updates as well as duplicate requests, including requests after lost replies.
  perform pg_advisory_xact_lock(471834920);
  select * into existing from public.learning_experiments where id=p_id for update;
  if found then
    if existing.revision = p_expected_revision + 1 and existing.payload = p_payload then
      return jsonb_build_object('revision',existing.revision);
    end if;
    if existing.revision <> p_expected_revision then raise exception 'Experiment revision conflict'; end if;
    if existing.payload->'config' <> p_payload->'config' or
       existing.payload->'scope' <> p_payload->'scope' or
       existing.payload->'learner' <> p_payload->'learner' or
       existing.payload->'seed' <> p_payload->'seed' then
      raise exception 'Experiment settings are immutable';
    end if;
    if existing.payload->'frozen' <> 'null'::jsonb and existing.payload->'frozen' <> p_payload->'frozen' then
      raise exception 'Frozen controller is immutable';
    end if;
    if existing.payload->>'phase' in ('complete','ended') and existing.payload <> p_payload then
      raise exception 'Experiment is already finished';
    end if;
  else
    if p_expected_revision <> 0 then raise exception 'Experiment not found'; end if;
    if exists(select 1 from public.learning_experiments where payload->>'phase' in ('training','evaluation')) then
      raise exception 'An experiment is already active';
    end if;
    insert into public.learning_experiments(id,revision,payload) values(p_id,1,p_payload);
  end if;
  if p_payload->>'id' <> p_id::text or p_payload->>'phase' not in ('training','evaluation','complete','ended') then
    raise exception 'Invalid experiment payload';
  end if;
  if p_run is not null then
    if p_run->>'experiment_id' <> p_id::text then raise exception 'Run belongs to another experiment'; end if;
    select payload into run_before from public.learning_experiment_runs where match_id=p_run->>'id';
    if found and run_before->>'status' <> 'selected' and run_before <> p_run then
      raise exception 'Run already finalized';
    end if;
    if p_run->>'phase' = 'evaluation' and (p_registration is not null or p_result is not null or p_checkpoint is not null) then
      raise exception 'Evaluation cannot train the controller';
    end if;
    insert into public.learning_experiment_runs(match_id,experiment_id,payload)
      values(p_run->>'id',p_id,p_run) on conflict(match_id) do update set payload=excluded.payload;
  end if;
  if p_registration is not null then
    if p_registration->>'id' <> p_run->>'id' or p_run->>'phase' <> 'training' then
      raise exception 'Invalid training registration';
    end if;
    catalog := p_payload->'strategies';
    perform public.learning_begin(p_registration,catalog);
  end if;
  if p_result is not null then
    if p_result->>'id' <> p_run->>'id' or p_run->>'phase' <> 'training' then
      raise exception 'Invalid training outcome';
    end if;
    result := public.learning_finish(p_result);
    if result->>'status' = 'scored' then
      if p_checkpoint is null then raise exception 'Missing experiment checkpoint'; end if;
      -- Experiment checkpoints include their immutable imported seed history.
      update public.learning_checkpoints set state=p_checkpoint where match_id=p_run->>'id';
    end if;
  end if;
  update public.learning_experiments set payload=p_payload, revision=p_expected_revision+1, updated_at=clock_timestamp()
    where id=p_id;
  return jsonb_build_object('revision',p_expected_revision+1);
end $$;
revoke all on function public.learning_checkpoint_catalog(),
  public.learning_experiment_commit(uuid,bigint,jsonb,jsonb,jsonb,jsonb,jsonb) from public,anon,authenticated;
grant execute on function public.learning_checkpoint_catalog(),
  public.learning_experiment_commit(uuid,bigint,jsonb,jsonb,jsonb,jsonb,jsonb) to service_role;
notify pgrst, 'reload schema';
commit;
