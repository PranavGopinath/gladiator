-- Apply after 202610040001. Rename existing objects without losing learned data.
begin;
do $$
declare suffix text; old_name text; new_name text; obj record; definition text;
begin
  foreach suffix in array array['strategies','matches','decisions','checkpoints'] loop
    old_name := 'gladiator_learning_' || suffix;
    new_name := 'learning_' || suffix;
    if to_regclass('public.' || old_name) is not null then
      if to_regclass('public.' || new_name) is not null then
        raise exception 'Both % and % exist; resolve the naming conflict before migrating', old_name, new_name;
      end if;
      execute format('alter table public.%I rename to %I', old_name, new_name);
    elsif to_regclass('public.' || new_name) is null then
      raise exception 'Missing learning tables; apply migration 202610040001 first';
    end if;
  end loop;

  -- Constraint renames also rename their associated primary/unique indexes.
  for obj in select c.conname, t.relname from pg_constraint c
    join pg_class t on t.oid = c.conrelid join pg_namespace n on n.oid = t.relnamespace
    where n.nspname = 'public' and t.relname in
      ('learning_strategies','learning_matches','learning_decisions','learning_checkpoints')
      and c.conname like 'gladiator_learning_%' loop
    execute format('alter table public.%I rename constraint %I to %I', obj.relname,
                   obj.conname, replace(obj.conname,'gladiator_learning_','learning_'));
  end loop;
  for obj in select indexname from pg_indexes where schemaname = 'public'
    and tablename in ('learning_strategies','learning_matches','learning_decisions','learning_checkpoints')
    and indexname like 'gladiator_learning_%' loop
    execute format('alter index public.%I rename to %I', obj.indexname,
                   replace(obj.indexname,'gladiator_learning_','learning_'));
  end loop;
  if to_regclass('public.gladiator_learning_checkpoints_id_seq') is not null then
    alter sequence public.gladiator_learning_checkpoints_id_seq rename to learning_checkpoints_id_seq;
  end if;

  -- PL/pgSQL bodies contain textual table references. Recreate with the new
  -- function and table names; a function rename alone would leave stale bodies.
  for obj in select p.oid, p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname in ('gladiator_learning_begin','gladiator_learning_finish') loop
    definition := replace(pg_get_functiondef(obj.oid),'gladiator_learning_','learning_');
    execute definition;
  end loop;
end $$;
revoke all on function public.learning_begin(jsonb,jsonb), public.learning_finish(jsonb) from public, anon, authenticated;
grant execute on function public.learning_begin(jsonb,jsonb), public.learning_finish(jsonb) to service_role;
drop function if exists public.gladiator_learning_begin(jsonb,jsonb);
drop function if exists public.gladiator_learning_finish(jsonb);
notify pgrst, 'reload schema';
commit;
