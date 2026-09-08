-- Private evidence storage for community reports. Moderation controls visibility.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'report-images',
    'report-images',
    false,
    5242880,
    array['image/jpeg', 'image/png', 'image/webp']::text[]
)
on conflict (id) do update set
    public = false,
    file_size_limit = 5242880,
    allowed_mime_types = array['image/jpeg', 'image/png', 'image/webp']::text[];

alter table public.community_reports add column if not exists image_data text;
alter table public.community_reports add column if not exists image_path text;

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'public.community_reports'::regclass
          and conname = 'reports_image_data_len'
    ) then
        alter table public.community_reports
            add constraint reports_image_data_len
            check (image_data is null or length(image_data) <= 7000000);
    end if;

    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'public.community_reports'::regclass
          and conname = 'reports_image_path_format'
    ) then
        alter table public.community_reports
            add constraint reports_image_path_format
            check (image_path is null or image_path ~ '^reports/[a-f0-9]{64}/[a-f0-9-]+\.(jpg|jpeg|png|webp)$');
    end if;
end
$$;
