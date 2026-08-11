/* =========================================================
   状態の保存先。

   Renderの無料プランには永続ディスクが無く、再起動でファイルが消える。
   一方で消えて困るもの（アカウント・実行履歴・出力した一覧表）は、
   全部あわせても数百KBのテキストしかない。大きいのは処理中の動画だけで、
   それは終われば捨てるものなので消えて構わない。
   よって「小さいテキストだけ外部DBへ、動画はローカルの使い捨て」とする。

   SUPABASE_URL と SUPABASE_SERVICE_KEY があればSupabase、
   無ければローカルのJSONファイル（開発用）。同じ関数で両方を扱う。
   ========================================================= */
'use strict';
const fs = require('fs');
const path = require('path');

/* SUPABASE_URL はホスト名だけを期待する（https://xxxx.supabase.co）。
   ただし画面から拾うと末尾に /rest/v1 やスラッシュが付いてくることがあり、
   そのまま繋ぐと /rest/v1/rest/v1/... になって PGRST125 で落ちる。
   原因が分かりにくい割に直し方は自明なので、ここで吸収する。 */
const URL_ = (process.env.SUPABASE_URL || '').trim()
  .replace(/\/+$/, '').replace(/\/rest\/v1$/, '').replace(/\/+$/, '');
/* キーは空白を全部落とす。長いので貼るときに折り返されることがあり、
   途中に改行が入るとHTTPヘッダに載せられず、原因の分かりにくい形で落ちる。
   キーの内部に空白が入ることは正当には有り得ないので、消して構わない。 */
const KEY = (process.env.SUPABASE_SERVICE_KEY || '').replace(/\s+/g, '');
const USE_DB = !!(URL_ && KEY);

/* テーブルは2つ。列を細かく分けず data(jsonb) に丸ごと入れる。
   アプリ側は全件をメモリに読み込んで使うのでSQLで検索する必要が無く、
   列を増やすたびにマイグレーションを書く手間のほうが高くつく。 */
const T_USERS = 'digest_users';
const T_JOBS = 'digest_jobs';

async function rest(pathQuery, init = {}) {
  const url = `${URL_}/rest/v1/${pathQuery}`;
  const r = await fetch(url, {
    ...init,
    headers: {
      apikey: KEY, Authorization: `Bearer ${KEY}`,
      'Content-Type': 'application/json', ...(init.headers || {}),
    },
  });
  // 失敗時は叩いた先も出す。キーの誤りかURLの誤りかテーブル未作成かで
  // 対処が全く違うのに、本文だけでは見分けが付かない。
  if (!r.ok) throw new Error(`Supabase ${r.status} (${url}): ${(await r.text()).slice(0, 300)}`);
  const body = await r.text();
  return body ? JSON.parse(body) : null;
}

const upsert = (table, rows) => rest(table, {
  method: 'POST',
  headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
  body: JSON.stringify(rows),
});

/* ---- ローカルファイル版 ---- */
function fileStore(dir) {
  const f = n => path.join(dir, n);
  const read = (n, d) => { try { return JSON.parse(fs.readFileSync(f(n), 'utf8')); } catch { return d; } };
  const write = (n, v) => { fs.writeFileSync(f(n) + '.tmp', JSON.stringify(v, null, 1)); fs.renameSync(f(n) + '.tmp', f(n)); };
  let users = null, jobs = [];
  return {
    kind: 'file',
    /* jobs は配列を複製して渡す。同じ配列を共有すると、putJob が保存用の
       strip した写しで要素を差し替えたときに、アプリ側が握っている実行中の
       ジョブ（videoPath や log を持つ）まで消えてしまう。 */
    async load() { users = read('users.json', null); jobs = read('jobs.json', []); return { users, jobs: jobs.slice() }; },
    async putUsers(list) { users = list; write('users.json', list); },
    async putJob(job) {
      const i = jobs.findIndex(j => j.id === job.id);
      if (i >= 0) jobs[i] = strip(job); else jobs.unshift(strip(job));
      write('jobs.json', jobs);
    },
  };
}

/* ---- Supabase版 ---- */
function dbStore() {
  return {
    kind: 'supabase',
    async load() {
      const [us, js] = await Promise.all([
        rest(`${T_USERS}?select=data`),
        // 全件抱える必要は無い。画面に出すのは直近だけ。
        rest(`${T_JOBS}?select=data&order=created_at.desc&limit=300`),
      ]);
      return {
        users: us.length ? us.map(r => r.data) : null,
        jobs: js.map(r => r.data),
      };
    },
    // 利用者数はたかが知れているので、変更のたびに全件書き直して整合を優先する。
    putUsers: list => upsert(T_USERS, list.map(u => ({ id: u.id, data: u }))),
    putJob: job => upsert(T_JOBS, [{
      id: job.id, user_id: job.user, created_at: job.createdAt, data: strip(job),
    }]),
  };
}

/* 保存しないもの。
   log は実行中しか意味が無く、行が増えるたびに書き戻すと通信が膨れる。
   videoPath は再起動後には存在しないパスなので残すとかえって紛らわしい。 */
function strip(job) {
  const { log, videoPath, ...rest } = job;
  return rest;
}

module.exports = { create: dir => (USE_DB ? dbStore() : fileStore(dir)), USE_DB };
