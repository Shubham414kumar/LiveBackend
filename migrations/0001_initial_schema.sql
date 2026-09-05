-- =============================================================================
-- SentinelAI — initial schema
-- =============================================================================
-- Apply in the Supabase SQL editor, or:
--   psql "$DATABASE_URL" -f backend/migrations/0001_initial_schema.sql
--
-- Idempotent: every statement is guarded, so re-running is safe.
--
-- -----------------------------------------------------------------------------
-- SECURITY MODEL — read this before changing anything below
-- -----------------------------------------------------------------------------
-- This app has no user accounts. Each install generates a UUID and sends it as
-- an `X-Device-Id` header. Ownership is therefore enforced in the application
-- layer (`app/db/repositories.py`), where every scoped query takes `device_id`
-- as a required argument.
--
-- Row Level Security is enabled on all four tables with **no policies at all**.
-- That is deliberate, and it is not the same as "we forgot the policies":
--
--   * RLS cannot scope these tables by device. A policy can only reference an
--     authenticated Postgres/Supabase identity, and there isn't one — a device
--     id arrives in an HTTP header the database never sees. A policy like
--     `USING (true)` would be strictly worse than none: it would make every
--     row readable by anyone holding the anon key.
--
--   * RLS with zero policies denies all access to the `anon` and
--     `authenticated` roles, while the `service_role` key bypasses RLS
--     entirely. So the backend keeps working, and a leaked anon key — the one
--     that would normally end up embedded in a mobile app — grants nothing.
--
-- Consequence: SUPABASE_KEY on the backend MUST be the **service_role** key,
-- and it must never be shipped to a client. If these tables ever need direct
-- client access, introduce real Supabase auth first and write policies against
-- `auth.uid()`; do not add permissive policies to make the anon key work.
-- =============================================================================

-- gen_random_uuid() is built in from PostgreSQL 13; the extension covers older
-- instances without erroring on newer ones.
create extension if not exists "pgcrypto";


-- =============================================================================
-- favorites — saved locations, one set per device
-- =============================================================================
create table if not exists public.favorites (
    id               uuid primary key default gen_random_uuid(),
    device_id        text        not null,
    name             text        not null,
    lat              double precision not null,
    lon              double precision not null,
    station_uid      text,
    alert_radius_km  double precision not null default 100,
    created_at       timestamptz not null default now(),

    constraint favorites_name_not_blank    check (length(btrim(name)) > 0),
    constraint favorites_lat_range         check (lat between -90 and 90),
    constraint favorites_lon_range         check (lon between -180 and 180),
    constraint favorites_radius_range      check (alert_radius_km > 0 and alert_radius_km <= 20000),
    constraint favorites_device_id_present check (length(btrim(device_id)) > 0)
);

-- Serves the only list query: this device's favourites, newest first.
create index if not exists favorites_device_created_idx
    on public.favorites (device_id, created_at desc);

-- Idempotent saves. The repository catches the unique violation (SQLSTATE 23505)
-- and returns the existing row, so tapping "save" twice is not an error.
-- Two indexes because a favourite may be identified either by AQI station or by
-- raw coordinates, and `station_uid` is frequently null (partial index).
create unique index if not exists favorites_device_station_uniq
    on public.favorites (device_id, station_uid)
    where station_uid is not null;

create unique index if not exists favorites_device_coords_uniq
    on public.favorites (device_id, lat, lon);


-- =============================================================================
-- community_reports — crowd-sourced hazard reports
-- =============================================================================
-- Post-moderation: a report is `visible` the moment it is filed. For a
-- public-safety feed, holding a flood report in a queue until a human approves
-- it defeats the purpose. Admins hide or remove after the fact.
create table if not exists public.community_reports (
    id            uuid primary key default gen_random_uuid(),
    device_id     text        not null,
    category      text        not null,
    title         text        not null,
    description   text,
    lat           double precision not null,
    lon           double precision not null,
    severity      text        not null default 'Moderate',
    status        text        not null default 'visible',
    upvotes       integer     not null default 0,
    moderated_by  text,
    moderated_at  timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),

    constraint reports_lat_range   check (lat between -90 and 90),
    constraint reports_lon_range   check (lon between -180 and 180),
    constraint reports_title_len   check (length(btrim(title)) between 1 and 200),
    constraint reports_desc_len    check (description is null or length(description) <= 2000),
    constraint reports_upvotes_pos check (upvotes >= 0),
    constraint reports_device_id_present check (length(btrim(device_id)) > 0),
    -- Mirrors the Literal types in app/models/schemas.py. Duplicated on purpose:
    -- validation at the edge gives a clean 422, and the constraint here means a
    -- direct SQL insert or a future writer cannot introduce a value the mobile
    -- client has no icon or colour for.
    constraint reports_severity_valid check (
        severity in ('Low', 'Moderate', 'Severe', 'Extreme')
    ),
    constraint reports_status_valid check (
        status in ('visible', 'hidden', 'removed')
    ),
    constraint reports_category_valid check (
        category in (
            'flood', 'fire', 'accident', 'road_block',
            'pollution', 'water_logging', 'disease_cluster', 'other'
        )
    )
);

-- The nearby-reports query filters status, then a lat/lon bounding box, then
-- created_at. Leading with status keeps the hidden and removed rows out before
-- any geometry is considered.
create index if not exists reports_status_bbox_idx
    on public.community_reports (status, lat, lon);

create index if not exists reports_status_created_idx
    on public.community_reports (status, created_at desc);

-- Backs the per-device daily quota check.
create index if not exists reports_device_created_idx
    on public.community_reports (device_id, created_at desc);

-- Backs the admin list, which filters by status and/or category.
create index if not exists reports_status_category_idx
    on public.community_reports (status, category);


-- =============================================================================
-- report_votes — one confirmation per device per report
-- =============================================================================
create table if not exists public.report_votes (
    report_id  uuid        not null
                   references public.community_reports (id) on delete cascade,
    device_id  text        not null,
    created_at timestamptz not null default now(),

    -- This composite primary key *is* the one-vote-per-device rule. Enforcing it
    -- in application code instead would let two concurrent requests both read
    -- "no existing vote" and both insert. The repository catches the resulting
    -- unique violation and reports "already confirmed" rather than failing.
    constraint report_votes_pkey primary key (report_id, device_id)
);

-- The PK's leading column already serves per-report counts. This one serves the
-- reverse lookup: "which of these reports has this device voted on", used to
-- render the vote button in the right state instead of letting the user tap it
-- and be rejected.
create index if not exists report_votes_device_report_idx
    on public.report_votes (device_id, report_id);


-- =============================================================================
-- push_tokens — one Expo token per device
-- =============================================================================
create table if not exists public.push_tokens (
    id         uuid primary key default gen_random_uuid(),
    device_id  text not null,
    token      text not null,
    platform   text,
    lat        double precision,
    lon        double precision,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    constraint push_tokens_lat_range check (lat is null or lat between -90 and 90),
    constraint push_tokens_lon_range check (lon is null or lon between -180 and 180),
    constraint push_tokens_platform_valid check (
        platform is null or platform in ('ios', 'android', 'web')
    ),
    constraint push_tokens_device_id_present check (length(btrim(device_id)) > 0)
);

-- Required by the repository's `upsert(..., on_conflict="device_id")`: without a
-- unique constraint on this column the upsert raises instead of replacing.
-- Keyed on the device, not the token, because an Expo token rotates on reinstall
-- and on some OS updates — keying on the token accumulates dead rows that fail
-- delivery forever.
create unique index if not exists push_tokens_device_uniq
    on public.push_tokens (device_id);

-- Supports future geo-targeted sends ("alert every device within N km").
create index if not exists push_tokens_coords_idx
    on public.push_tokens (lat, lon)
    where lat is not null and lon is not null;


-- =============================================================================
-- updated_at maintenance
-- =============================================================================
-- A trigger rather than trusting every writer to set it. The repositories do set
-- `updated_at`, but a manual fix applied through the Supabase table editor
-- would otherwise leave it stale and quietly break "what changed recently".
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists community_reports_touch_updated_at on public.community_reports;
create trigger community_reports_touch_updated_at
    before update on public.community_reports
    for each row execute function public.touch_updated_at();

drop trigger if exists push_tokens_touch_updated_at on public.push_tokens;
create trigger push_tokens_touch_updated_at
    before update on public.push_tokens
    for each row execute function public.touch_updated_at();


-- =============================================================================
-- Row Level Security — deny-by-default (see the note at the top of this file)
-- =============================================================================
alter table public.favorites         enable row level security;
alter table public.community_reports enable row level security;
alter table public.report_votes      enable row level security;
alter table public.push_tokens       enable row level security;

-- `force` also subjects the tables' owner to RLS, so a mistake made while
-- connected as the owner cannot silently read everything.
alter table public.favorites         force row level security;
alter table public.community_reports force row level security;
alter table public.report_votes      force row level security;
alter table public.push_tokens       force row level security;

-- Deliberately no CREATE POLICY statements. `service_role` bypasses RLS; every
-- other role is denied. Adding `USING (true)` here would expose every device's
-- data to anyone holding the anon key.

-- Belt and braces: revoke the grants Supabase hands the public API roles by
-- default, so these tables are unreachable over the auto-generated REST API even
-- if RLS is later disabled by accident.
revoke all on public.favorites         from anon, authenticated;
revoke all on public.community_reports from anon, authenticated;
revoke all on public.report_votes      from anon, authenticated;
revoke all on public.push_tokens       from anon, authenticated;


-- =============================================================================
-- Retention
-- =============================================================================
-- Hazard reports are time-critical and worthless once stale; the API already
-- refuses to serve anything older than 30 days. Keeping the rows forever grows
-- the table without ever being read, and retaining device ids longer than they
-- are useful is a liability rather than an asset.
--
-- Not scheduled automatically here, because pg_cron availability varies by
-- Supabase plan. Either enable pg_cron and use the commented schedule below, or
-- call this from any external scheduler.
create or replace function public.prune_old_data(retain_days integer default 30)
returns table (reports_deleted bigint, tokens_deleted bigint)
language plpgsql
security definer
set search_path = public
as $$
declare
    cutoff timestamptz := now() - make_interval(days => retain_days);
    r bigint;
    t bigint;
begin
    -- Votes disappear with their report through the ON DELETE CASCADE above.
    delete from public.community_reports where created_at < cutoff;
    get diagnostics r = row_count;

    -- A token untouched for six months belongs to an uninstalled app; Expo will
    -- reject it and every send wastes a request.
    delete from public.push_tokens where updated_at < now() - interval '180 days';
    get diagnostics t = row_count;

    return query select r, t;
end;
$$;

-- With pg_cron available, run nightly at 03:15 UTC:
--   create extension if not exists pg_cron;
--   select cron.schedule('sentinelai-prune', '15 3 * * *',
--                        $$select public.prune_old_data(30)$$);
