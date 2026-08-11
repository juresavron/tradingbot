-- Trading bot dashboard schema.
--
-- Design: the dashboard (browser, logged in via Supabase Auth) only READS,
-- and only the signed-in user's own rows (row-level security). The bot
-- (Python, on your machine) only WRITES, and only through the ingest()
-- function using a per-run secret key -- it never holds your login.
-- Nobody can write tables directly: there are no INSERT/UPDATE policies.

create extension if not exists pgcrypto;

create table public.runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  name text not null default 'paper bot',
  symbol text not null default '',
  timeframe text not null default '',
  strategy text not null default '',
  starting_cash numeric not null default 100,
  ingest_key_hash text not null,
  killed boolean not null default false,
  created_at timestamptz not null default now(),
  last_seen_at timestamptz
);

create table public.equity_points (
  run_id uuid not null references public.runs (id) on delete cascade,
  ts bigint not null,
  equity numeric not null,
  price numeric,
  primary key (run_id, ts)
);

create table public.trades (
  run_id uuid not null references public.runs (id) on delete cascade,
  seq integer not null,
  ts bigint not null,
  side text not null,
  price numeric not null,
  qty numeric not null,
  pnl numeric,
  reason text not null default 'signal',
  primary key (run_id, seq)
);

alter table public.runs enable row level security;
alter table public.equity_points enable row level security;
alter table public.trades enable row level security;

create policy "owners read their runs"
  on public.runs for select to authenticated
  using (user_id = (select auth.uid()));

create policy "owners delete their runs"
  on public.runs for delete to authenticated
  using (user_id = (select auth.uid()));

create policy "owners read their equity"
  on public.equity_points for select to authenticated
  using (run_id in (select id from public.runs where user_id = (select auth.uid())));

create policy "owners read their trades"
  on public.trades for select to authenticated
  using (run_id in (select id from public.runs where user_id = (select auth.uid())));

-- Called from the dashboard when you click "New run". Generates the run's
-- secret ingest key server-side; the plaintext key is returned exactly once.
create or replace function public.create_run(
  p_name text,
  p_symbol text,
  p_timeframe text,
  p_strategy text,
  p_starting_cash numeric
) returns table (run_id uuid, ingest_key text)
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_key text;
  v_id uuid;
begin
  if (select auth.uid()) is null then
    raise exception 'must be signed in';
  end if;
  v_key := encode(gen_random_bytes(24), 'hex');
  insert into runs (user_id, name, symbol, timeframe, strategy, starting_cash, ingest_key_hash)
  values (
    (select auth.uid()),
    coalesce(nullif(p_name, ''), 'paper bot'),
    coalesce(p_symbol, ''),
    coalesce(p_timeframe, ''),
    coalesce(p_strategy, ''),
    coalesce(p_starting_cash, 100),
    encode(digest(v_key, 'sha256'), 'hex')
  )
  returning id into v_id;
  return query select v_id, v_key;
end
$$;

revoke all on function public.create_run(text, text, text, text, numeric) from public;
grant execute on function public.create_run(text, text, text, text, numeric) to authenticated;

-- Called by the bot. Authenticates with the per-run secret key, never a login.
-- Upserts make it idempotent: re-pushing the same points/trades is harmless.
create or replace function public.ingest(
  p_run_id uuid,
  p_key text,
  p_equity jsonb default '[]'::jsonb,   -- [[ts_ms, equity, price|null], ...]
  p_trades jsonb default '[]'::jsonb,   -- [{seq, ts, side, price, qty, pnl, reason}, ...]
  p_killed boolean default null,
  p_meta jsonb default null             -- {symbol, timeframe, strategy, starting_cash}
) returns void
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_hash text;
begin
  select ingest_key_hash into v_hash from runs where id = p_run_id;
  if v_hash is null or v_hash <> encode(digest(p_key, 'sha256'), 'hex') then
    raise exception 'invalid run credentials';
  end if;

  insert into equity_points (run_id, ts, equity, price)
  select p_run_id, (e ->> 0)::bigint, (e ->> 1)::numeric, (e ->> 2)::numeric
  from jsonb_array_elements(p_equity) e
  on conflict (run_id, ts) do update
    set equity = excluded.equity,
        price = coalesce(excluded.price, equity_points.price);

  insert into trades (run_id, seq, ts, side, price, qty, pnl, reason)
  select
    p_run_id,
    (t ->> 'seq')::integer,
    (t ->> 'ts')::bigint,
    t ->> 'side',
    (t ->> 'price')::numeric,
    (t ->> 'qty')::numeric,
    (t ->> 'pnl')::numeric,
    coalesce(t ->> 'reason', 'signal')
  from jsonb_array_elements(p_trades) t
  on conflict (run_id, seq) do nothing;

  update runs set
    last_seen_at = now(),
    killed = coalesce(p_killed, killed),
    symbol = coalesce(p_meta ->> 'symbol', symbol),
    timeframe = coalesce(p_meta ->> 'timeframe', timeframe),
    strategy = coalesce(p_meta ->> 'strategy', strategy),
    starting_cash = coalesce((p_meta ->> 'starting_cash')::numeric, starting_cash)
  where id = p_run_id;
end
$$;

revoke all on function public.ingest(uuid, text, jsonb, jsonb, boolean, jsonb) from public;
grant execute on function public.ingest(uuid, text, jsonb, jsonb, boolean, jsonb) to anon, authenticated;
