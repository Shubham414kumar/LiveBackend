-- =============================================================================
-- SentinelAI — 0002: alert delivery
-- =============================================================================
--   psql "$DATABASE_URL" -f backend/migrations/0002_alert_delivery.sql
--
-- Idempotent, like 0001: every statement is guarded, so re-running is safe.
-- Apply after 0001_initial_schema.sql.
--
-- -----------------------------------------------------------------------------
-- WHY THIS MIGRATION EXISTS
-- -----------------------------------------------------------------------------
-- Until now the app registered push tokens and never sent a single
-- notification: `POST /api/notifications/register` stored a token,
-- `GET /api/favorites/alerts` matched saved locations against the disaster feed,
-- and the two were never connected. An alerting app whose alerts only arrive
-- when the user opens it is not an alerting app.
--
-- Sending is the easy half. The hard half is not waking someone at 3 a.m. four
-- times for one earthquake, and that is what the two changes here are for:
--
--   * `sent_alerts` makes delivery idempotent. Its composite primary key is the
--     dedupe rule, so the same event can never be delivered twice to the same
--     device even if two dispatcher passes overlap.
--
--   * preference columns on `push_tokens` let each user set a severity floor and
--     a quiet window, so the volume is theirs to control rather than ours to
--     guess.
--
-- Same security posture as 0001: RLS on, forced, no policies, grants revoked.
-- The backend reaches these tables with the service_role key, which bypasses
-- RLS; anon and authenticated are denied everything.
-- =============================================================================


-- =============================================================================
-- sent_alerts — one row per (device, event) ever delivered
-- =============================================================================
create table if not exists public.sent_alerts (
    device_id   text        not null,
    -- The event id from the merged feed: "us7000abcd" (USGS), "gdacs_EQ_1234",
    -- "eonet_EONET_5678", "pandemic_IN". Text, not a foreign key: these events
    -- live in third-party feeds and are never stored in this database.
    event_id    text        not null,
    -- 'pending' is written *before* the send is attempted, so a crash mid-send
    -- leaves a claim behind rather than a silent double-delivery on the next
    -- pass. 'failed' rows are retried a bounded number of times; 'sent' rows
    -- are never touched again.
    status      text        not null default 'pending',
    attempts    integer     not null default 0,
    -- Exception class name or Expo error code. Never the token, never a body.
    last_error  text,
    claimed_at  timestamptz not null default now(),
    sent_at     timestamptz,

    -- This composite primary key *is* the idempotency rule, and it is here
    -- rather than in application code for the same reason report_votes' key is:
    -- two dispatcher passes running concurrently would both read "not yet sent"
    -- and both send. The dispatcher inserts with ON CONFLICT DO NOTHING and
    -- treats the returned rows as its claim, so exactly one pass wins.
    constraint sent_alerts_pkey primary key (device_id, event_id),

    constraint sent_alerts_device_id_present check (length(btrim(device_id)) > 0),
    constraint sent_alerts_event_id_present  check (length(btrim(event_id)) > 0),
    constraint sent_alerts_attempts_positive check (attempts >= 0),
    constraint sent_alerts_status_valid check (
        status in ('pending', 'sent', 'failed')
    )
);

-- Backs the retry sweep: "claims old enough to be worth another attempt".
--
-- Keyed on claimed_at because that is both the sweep's range filter (only a
-- claim older than one dispatch interval can have been abandoned by a crashed
-- pass) and its sort order (oldest first). `attempts < max` is left as a filter
-- on the heap: the partial index holds only unsettled claims, which in a healthy
-- deployment is a handful of rows, so narrowing it further buys nothing.
drop index if exists public.sent_alerts_retry_idx;
create index if not exists sent_alerts_retry_idx
    on public.sent_alerts (claimed_at)
    where status <> 'sent';

-- Backs pruning, and the per-device volume check the dispatcher runs before it
-- sends ("how many alerts has this device already had in the last hour").
create index if not exists sent_alerts_device_claimed_idx
    on public.sent_alerts (device_id, claimed_at desc);


-- =============================================================================
-- push_tokens — delivery preferences
-- =============================================================================
-- Added as columns on the existing table rather than as a separate
-- `notification_preferences` table. There is exactly one token row per device
-- (see the unique index in 0001), the dispatcher reads token and preferences
-- together on every pass, and a second table would only add a join to every
-- read and an orphan-row failure mode to every write.

-- A master switch that is not "delete the token". Turning alerts off and back
-- on must not require the OS permission prompt again, and on iOS a re-prompt
-- after a denial cannot be triggered from the app at all.
alter table public.push_tokens
    add column if not exists alerts_enabled boolean not null default true;

-- The severity floor. Default 'Moderate' rather than 'Low': USGS reports
-- magnitude 4.5 quakes as Low or Minor, and a user within 100 km of an active
-- fault would receive several a week and mute the app inside a month.
alter table public.push_tokens
    add column if not exists min_severity text not null default 'Moderate';

-- Quiet hours, as local wall-clock hours [start, end). Stored as hours rather
-- than timestamps because the user's intent is "not while I'm asleep", which
-- recurs daily and must survive them travelling across a time zone.
--
-- A window may wrap midnight: start 22, end 7 means 22:00–07:00. That is the
-- common case, so it has to work rather than be rejected.
alter table public.push_tokens
    add column if not exists quiet_hours_start smallint;
alter table public.push_tokens
    add column if not exists quiet_hours_end smallint;

-- IANA zone name, e.g. 'Asia/Kolkata'. Required to interpret the two columns
-- above. When it is null the dispatcher does not apply quiet hours at all —
-- it delivers. Guessing UTC would silence a user in IST for the wrong nine
-- hours of their day, and silently mistimed suppression is worse than none.
alter table public.push_tokens
    add column if not exists timezone text;

-- Set once, then repeated in the constraints below. `add constraint if not
-- exists` does not exist in PostgreSQL, so each is dropped first — which also
-- makes re-running this file pick up a changed definition instead of skipping it.
alter table public.push_tokens
    drop constraint if exists push_tokens_min_severity_valid;
alter table public.push_tokens
    add constraint push_tokens_min_severity_valid check (
        min_severity in ('Low', 'Minor', 'Moderate', 'Severe', 'Extreme')
    );

alter table public.push_tokens
    drop constraint if exists push_tokens_quiet_hours_range;
alter table public.push_tokens
    add constraint push_tokens_quiet_hours_range check (
        (quiet_hours_start is null or quiet_hours_start between 0 and 23)
        and (quiet_hours_end is null or quiet_hours_end between 0 and 23)
    );

-- Both ends or neither. A half-specified window has no defensible reading, and
-- the alternative to rejecting it is the dispatcher inventing the missing half.
alter table public.push_tokens
    drop constraint if exists push_tokens_quiet_hours_paired;
alter table public.push_tokens
    add constraint push_tokens_quiet_hours_paired check (
        (quiet_hours_start is null) = (quiet_hours_end is null)
    );

-- The dispatcher's main scan: every device that can actually be sent to, walked
-- in pages. Partial, because a row with alerts off is never a candidate and
-- there is no reason to keep it in the index.
--
-- Keyed on device_id, not updated_at, because the dispatcher paginates by
-- ascending device_id. It writes to these same rows while it walks them — a
-- successful send touches updated_at, and so does the app re-registering
-- mid-pass — so a window ordered by updated_at would visit a moved row twice or
-- skip it entirely. Skipping means a missed alert. device_id is unique and
-- immutable, so a keyset on it is stable no matter what the pass writes.
--
-- Dropped first for the same reason the constraints above are: `create index if
-- not exists` skips silently, so re-running this file after the definition
-- changed would leave the old index in place.
drop index if exists public.push_tokens_dispatchable_idx;
create index if not exists push_tokens_dispatchable_idx
    on public.push_tokens (device_id)
    where alerts_enabled;


-- =============================================================================
-- Row Level Security — deny-by-default, as in 0001
-- =============================================================================
alter table public.sent_alerts enable row level security;
alter table public.sent_alerts force  row level security;

-- Deliberately no CREATE POLICY. service_role bypasses RLS; every other role is
-- denied. `USING (true)` here would let anyone holding the anon key enumerate
-- which devices were alerted about which events, and when.
revoke all on public.sent_alerts from anon, authenticated;


-- =============================================================================
-- Retention
-- =============================================================================
-- `create or replace function` cannot change a function's return type, and this
-- version returns a third column, so the old one is dropped first. Dropping by
-- explicit signature so this cannot match some other overload added later.
drop function if exists public.prune_old_data(integer);

create or replace function public.prune_old_data(retain_days integer default 30)
returns table (reports_deleted bigint, tokens_deleted bigint, sent_alerts_deleted bigint)
language plpgsql
security definer
set search_path = public
as $$
declare
    cutoff timestamptz := now() - make_interval(days => retain_days);
    r bigint;
    t bigint;
    s bigint;
begin
    -- Votes disappear with their report through the ON DELETE CASCADE in 0001.
    delete from public.community_reports where created_at < cutoff;
    get diagnostics r = row_count;

    -- A token untouched for six months belongs to an uninstalled app; Expo will
    -- reject it and every send wastes a request.
    delete from public.push_tokens where updated_at < now() - interval '180 days';
    get diagnostics t = row_count;

    -- Dedupe records outlive their usefulness with the event they describe. The
    -- dispatcher only ever looks at events from the last few days, so a claim
    -- older than the retention window can never suppress a live alert — but it
    -- is still a record of what a device was told, so it is not kept forever.
    delete from public.sent_alerts where claimed_at < cutoff;
    get diagnostics s = row_count;

    return query select r, t, s;
end;
$$;

-- With pg_cron available, run nightly at 03:15 UTC:
--   create extension if not exists pg_cron;
--   select cron.schedule('sentinelai-prune', '15 3 * * *',
--                        $$select public.prune_old_data(30)$$);
