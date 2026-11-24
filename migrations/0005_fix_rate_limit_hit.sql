-- Fixes public.rate_limit_hit from 0003. The variable window_start had the same name as the
-- rate_limits column used in "on conflict (key, window_start)", so Postgres refused every call with
-- error 42702 (ambiguous column reference). The variable is
-- renamed; nothing else changes. Safe to re-run.
create or replace function public.rate_limit_hit(p_key text, p_window_seconds integer, p_max integer)
returns boolean
language plpgsql
set search_path = public
as $$
declare
  current_window timestamptz;
  new_hits integer;
begin
  if p_window_seconds < 1 then
    raise exception 'p_window_seconds must be at least 1';
  end if;
  current_window := to_timestamp(floor(extract(epoch from now()) / p_window_seconds) * p_window_seconds);
  insert into public.rate_limits as r (key, window_start, hits)
  values (p_key, current_window, 1)
  on conflict (key, window_start) do update set hits = r.hits + 1
  returning r.hits into new_hits;
  return new_hits <= p_max;
end;
$$;

-- Same privileges as the other engine-only functions: the browser roles may not call it.
revoke all on function public.rate_limit_hit(text, integer, integer) from public, anon, authenticated;
