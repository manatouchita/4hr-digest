-- =========================================================
-- Supabase 初期セットアップ（1回だけ実行する）
--
-- Supabaseの画面 → SQL Editor に貼り付けて Run。
-- 何度実行しても壊れないように書いてあるので、迷ったら再実行してよい。
--
-- 列を細かく分けていないのは、アプリが全件をメモリに読み込んで使い、
-- SQLで検索しないため。列を増やすたびに移行作業が要る構成のほうが、
-- この規模では手間が大きい。検索・集計が必要になったら data->>'...' で足りる。
-- =========================================================

-- アカウント。数人〜十数人。
create table if not exists digest_users (
  id   text primary key,       -- ログインID
  data jsonb not null          -- 表示名・権限・パスワードハッシュ・停止フラグ等
);

-- 実行履歴。CSV本文（数KB）も data に入っている。
create table if not exists digest_jobs (
  id         text primary key,      -- ジョブID（8桁）
  user_id    text,                  -- 実行した人
  created_at timestamptz,           -- 並び替え用。data の中にも同じ値がある
  data       jsonb not null
);

-- 一覧は「新しい順に300件」しか読まないので、その並びだけ速ければよい。
create index if not exists digest_jobs_created_idx
  on digest_jobs (created_at desc);

-- =========================================================
-- 行レベルセキュリティ
--
-- アプリはサービスキーで接続する。サービスキーはRLSを迂回するので
-- アプリの動作には影響しないが、有効にしておかないと、万一 anon キーが
-- 漏れたときに誰でもパスワードハッシュを読めてしまう。
-- ポリシーを1つも作らない = サービスキー以外は読み書きできない。
-- =========================================================
alter table digest_users enable row level security;
alter table digest_jobs  enable row level security;
