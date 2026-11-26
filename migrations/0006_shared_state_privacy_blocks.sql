-- Shared chat state for several engine workers, the owner's block list, and a deletion period for chat text.
-- Safe to re-run.
--
-- Privacy: like the other engine tables these are closed to the browser roles; the engine uses the service key.

-- One row per chat: its saved history (ciphertext when CHAT_ENCRYPTION_KEY is set) and which worker is
-- answering it right now. A claim lapses at locked_until, so a worker that dies cannot hold a chat for ever.
create table if not exists public.chat_state (
  chat_id bigint primary key,
  state text,
  locked_by text,
  locked_until timestamptz,
  updated_at timestamptz not null default now()
);
create index if not exists chat_state_updated_at_idx on public.chat_state (updated_at);

-- Chats the owner has blocked. lead_id is the lead the block was made from, which is how it is undone.
create table if not exists public.blocked_chats (
  chat_id bigint primary key,
  lead_id bigint not null,
  created_at timestamptz not null default now()
);
create index if not exists blocked_chats_lead_idx on public.blocked_chats (lead_id);

create index if not exists chat_turns_created_at_idx on public.chat_turns (created_at);

alter table public.chat_state enable row level security;
alter table public.blocked_chats enable row level security;
revoke all on public.chat_state, public.blocked_chats from anon, authenticated;

-- Claim a chat for one worker. One statement decides, so two workers cannot both win. The holder may claim
-- again to extend its own claim.
create or replace function public.claim_chat(p_chat bigint, p_owner text, p_seconds integer)
returns boolean
language plpgsql
set search_path = public
as $$
declare
  claimed bigint;
begin
  if p_seconds < 1 then
    raise exception 'p_seconds must be at least 1';
  end if;
  insert into public.chat_state as s (chat_id, locked_by, locked_until)
  values (p_chat, p_owner, now() + make_interval(secs => p_seconds))
  on conflict (chat_id) do update set locked_by = excluded.locked_by, locked_until = excluded.locked_until
    where s.locked_until is null or s.locked_until < now() or s.locked_by = excluded.locked_by
  returning s.chat_id into claimed;
  return claimed is not null;
end;
$$;

create or replace function public.release_chat(p_chat bigint, p_owner text)
returns void
language sql
set search_path = public
as $$
  update public.chat_state set locked_by = null, locked_until = null
  where chat_id = p_chat and locked_by = p_owner;
$$;

-- As before, plus: chat text older than p_text_days is deleted (0 keeps it). A chat being answered right now
-- is left alone.
drop function if exists public.purge_expired(integer, integer);
create or replace function public.purge_expired(p_update_days integer, p_lead_chat_days integer, p_text_days integer default 90)
returns void
language plpgsql
set search_path = public
as $$
begin
  delete from public.processed_updates where created_at < now() - make_interval(days => p_update_days);
  delete from public.rate_limits where window_start < now() - interval '1 day';
  delete from public.alert_messages where lead_id in (
    select lead_id from public.lead_chats where created_at < now() - make_interval(days => p_lead_chat_days));
  delete from public.lead_flags where lead_id in (
    select lead_id from public.lead_chats where created_at < now() - make_interval(days => p_lead_chat_days));
  delete from public.lead_chats where created_at < now() - make_interval(days => p_lead_chat_days);
  if p_text_days > 0 then
    delete from public.chat_turns where created_at < now() - make_interval(days => p_text_days);
    delete from public.conversation_turns where created_at < now() - make_interval(days => p_text_days);
    delete from public.chat_state where updated_at < now() - make_interval(days => p_text_days)
      and (locked_until is null or locked_until < now());
  end if;
end;
$$;

revoke all on function public.claim_chat(bigint, text, integer) from public, anon, authenticated;
revoke all on function public.release_chat(bigint, text) from public, anon, authenticated;
revoke all on function public.purge_expired(integer, integer, integer) from public, anon, authenticated;
grant execute on function public.claim_chat(bigint, text, integer) to service_role;
grant execute on function public.release_chat(bigint, text) to service_role;
grant execute on function public.purge_expired(integer, integer, integer) to service_role;
