-- Local Agent Studio: Supabase schema.
-- Run once in your project's SQL Editor (Dashboard -> SQL Editor -> New query -> Run).
-- Safe to run again: tables are created only if missing and policies are replaced.
--
-- Every row belongs to the signed-in user who wrote it. Row-level security lets each
-- account read and change only its own bots, groups, messages, and images.

create table if not exists public.bots (
  owner_id uuid not null default auth.uid() references auth.users (id) on delete cascade,
  id text not null,
  data jsonb not null,
  created_at timestamptz not null default now(),
  primary key (owner_id, id)
);

create table if not exists public.groups (
  owner_id uuid not null default auth.uid() references auth.users (id) on delete cascade,
  id text not null,
  name text not null check (char_length(name) between 1 and 100),
  mode text not null default 'conversation' check (mode in ('workflow', 'conversation')),
  master_id text not null default '',
  quality_criteria text not null default '' check (char_length(quality_criteria) <= 4000),
  bot_ids text[] not null default '{}',
  last_workflow jsonb,
  created_at timestamptz not null default now(),
  primary key (owner_id, id)
);

create table if not exists public.messages (
  owner_id uuid not null default auth.uid() references auth.users (id) on delete cascade,
  id text not null,
  group_id text not null,
  seq bigint generated always as identity,
  sender text not null,
  bot_id text not null default '',
  text text not null,
  image text,
  files jsonb not null default '[]',
  created_at timestamptz not null default now(),
  primary key (owner_id, id),
  foreign key (owner_id, group_id) references public.groups (owner_id, id) on delete cascade
);

create index if not exists messages_group_order on public.messages (owner_id, group_id, seq);

-- Message totals per group, so the app can show counts without loading every message.
-- security_invoker applies the messages table's row-level security to whoever reads the view.
create or replace view public.message_counts with (security_invoker = true) as
  select owner_id, group_id, count(*) as message_count from public.messages group by owner_id, group_id;
grant select on public.message_counts to authenticated;

-- Recent conversation and workflow runs, so their outcome survives an app restart.
create table if not exists public.runs (
  owner_id uuid not null default auth.uid() references auth.users (id) on delete cascade,
  id text not null,
  group_id text not null,
  data jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (owner_id, id),
  foreign key (owner_id, group_id) references public.groups (owner_id, id) on delete cascade
);

create index if not exists runs_recent on public.runs (owner_id, updated_at desc);

-- Saved API keys, encrypted on the user's computer before they are sent. Only the token,
-- the last four characters, and the save date are stored; the passphrase never is.
create table if not exists public.bot_keys (
  owner_id uuid not null default auth.uid() references auth.users (id) on delete cascade,
  bot_id text not null,
  token text not null,
  hint text not null default '',
  saved_at timestamptz not null default now(),
  primary key (owner_id, bot_id),
  foreign key (owner_id, bot_id) references public.bots (owner_id, id) on delete cascade
);

-- The passphrase setup: salts, scrypt settings, and the data key wrapped by the passphrase and the recovery code.
create table if not exists public.key_protection (
  owner_id uuid primary key default auth.uid() references auth.users (id) on delete cascade,
  data jsonb not null,
  updated_at timestamptz not null default now()
);

-- Documents attached to a chat: the text read from each file once, kept as context for its bots.
-- The original file is in the private documents bucket below.
create table if not exists public.documents (
  owner_id uuid not null default auth.uid() references auth.users (id) on delete cascade,
  id text not null,
  group_id text not null,
  name text not null check (char_length(name) between 1 and 255),
  kind text not null check (kind in ('pdf', 'docx', 'txt')),
  size bigint not null default 0,
  pages integer not null default 0,
  text text not null,
  warnings jsonb not null default '[]',
  file text not null,
  created_at timestamptz not null default now(),
  primary key (owner_id, id),
  foreign key (owner_id, group_id) references public.groups (owner_id, id) on delete cascade
);

create index if not exists documents_by_group on public.documents (owner_id, group_id);

alter table public.bots enable row level security;
alter table public.groups enable row level security;
alter table public.messages enable row level security;
alter table public.runs enable row level security;
alter table public.bot_keys enable row level security;
alter table public.key_protection enable row level security;
alter table public.documents enable row level security;

drop policy if exists "Owners manage their bots" on public.bots;
create policy "Owners manage their bots" on public.bots for all to authenticated
  using (owner_id = (select auth.uid())) with check (owner_id = (select auth.uid()));

drop policy if exists "Owners manage their groups" on public.groups;
create policy "Owners manage their groups" on public.groups for all to authenticated
  using (owner_id = (select auth.uid())) with check (owner_id = (select auth.uid()));

drop policy if exists "Owners manage their messages" on public.messages;
create policy "Owners manage their messages" on public.messages for all to authenticated
  using (owner_id = (select auth.uid())) with check (owner_id = (select auth.uid()));

drop policy if exists "Owners manage their runs" on public.runs;
create policy "Owners manage their runs" on public.runs for all to authenticated
  using (owner_id = (select auth.uid())) with check (owner_id = (select auth.uid()));

drop policy if exists "Owners manage their bot keys" on public.bot_keys;
create policy "Owners manage their bot keys" on public.bot_keys for all to authenticated
  using (owner_id = (select auth.uid())) with check (owner_id = (select auth.uid()));

drop policy if exists "Owners manage their key protection" on public.key_protection;
create policy "Owners manage their key protection" on public.key_protection for all to authenticated
  using (owner_id = (select auth.uid())) with check (owner_id = (select auth.uid()));

drop policy if exists "Owners manage their documents" on public.documents;
create policy "Owners manage their documents" on public.documents for all to authenticated
  using (owner_id = (select auth.uid())) with check (owner_id = (select auth.uid()));

-- Generated images: a private bucket with one folder per user (images/<user id>/<file>).
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('images', 'images', false, 20971520, array['image/png', 'image/jpeg', 'image/gif', 'image/webp'])
on conflict (id) do nothing;

drop policy if exists "Owners read their images" on storage.objects;
create policy "Owners read their images" on storage.objects for select to authenticated
  using (bucket_id = 'images' and (storage.foldername(name))[1] = (select auth.uid())::text);

drop policy if exists "Owners add their images" on storage.objects;
create policy "Owners add their images" on storage.objects for insert to authenticated
  with check (bucket_id = 'images' and (storage.foldername(name))[1] = (select auth.uid())::text);

drop policy if exists "Owners replace their images" on storage.objects;
create policy "Owners replace their images" on storage.objects for update to authenticated
  using (bucket_id = 'images' and (storage.foldername(name))[1] = (select auth.uid())::text)
  with check (bucket_id = 'images' and (storage.foldername(name))[1] = (select auth.uid())::text);

drop policy if exists "Owners delete their images" on storage.objects;
create policy "Owners delete their images" on storage.objects for delete to authenticated
  using (bucket_id = 'images' and (storage.foldername(name))[1] = (select auth.uid())::text);

-- Original files of attached documents: a private bucket with one folder per user (documents/<user id>/<file>).
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('documents', 'documents', false, 26214400,
        array['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'text/plain'])
on conflict (id) do nothing;

drop policy if exists "Owners read their documents" on storage.objects;
create policy "Owners read their documents" on storage.objects for select to authenticated
  using (bucket_id = 'documents' and (storage.foldername(name))[1] = (select auth.uid())::text);

drop policy if exists "Owners add their documents" on storage.objects;
create policy "Owners add their documents" on storage.objects for insert to authenticated
  with check (bucket_id = 'documents' and (storage.foldername(name))[1] = (select auth.uid())::text);

drop policy if exists "Owners replace their documents" on storage.objects;
create policy "Owners replace their documents" on storage.objects for update to authenticated
  using (bucket_id = 'documents' and (storage.foldername(name))[1] = (select auth.uid())::text)
  with check (bucket_id = 'documents' and (storage.foldername(name))[1] = (select auth.uid())::text);

drop policy if exists "Owners delete their documents" on storage.objects;
create policy "Owners delete their documents" on storage.objects for delete to authenticated
  using (bucket_id = 'documents' and (storage.foldername(name))[1] = (select auth.uid())::text);
