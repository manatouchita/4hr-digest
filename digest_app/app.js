/* =========================================================
   4HR ダイジェスト見どころ抽出アプリ（依存ゼロ / Node 18+）

   起動:  node digest_app/app.js  →  http://localhost:3100

   編集者が完成版HRをアップロードすると、見どころ候補の一覧表（xlsx）が出る。
   処理そのものは tools/digest_poc.py をそのまま呼ぶ。ロジックを
   JavaScriptに移植して二重管理にすると、CLIとWebで挙動がずれる。

   構成:
     /            アップロード画面（要ログイン）
     /admin       アカウント発行・全実行履歴（adminのみ）
     /login /logout
   ========================================================= */
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const qs = require('querystring');
const { spawn } = require('child_process');
const store = require('./store');

const ROOT = __dirname;
const PROJECT_ROOT = path.resolve(ROOT, '..');
const VIEWS = p => path.join(ROOT, 'views', p);
const PORT = process.env.PORT || 3100;
const PYTHON = process.env.PYTHON || 'python3';

/* 動画とPythonの出力を置く場所。どちらも使い捨てで、消えても再実行すれば済む。
   永続化が要るもの（アカウント・履歴・出力した一覧表）は store.js が見る。 */
const WORK_DIR = process.env.DIGEST_WORK_DIR || path.join(ROOT, 'data');
const UPLOAD_DIR = path.join(WORK_DIR, 'uploads');
const OUT_DIR = path.join(WORK_DIR, 'outputs');
for (const d of [WORK_DIR, UPLOAD_DIR, OUT_DIR]) fs.mkdirSync(d, { recursive: true });

/* 4GB。完成版HRは数百MBなので十分だが、無制限にすると事故でディスクが埋まる。 */
const MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024;
const COOKIE_SUFFIX = process.env.NODE_ENV === 'production' ? '; Secure' : '';

const DB = store.create(WORK_DIR);
const sha = s => crypto.createHash('sha256').update(String(s)).digest('hex');
const now = () => new Date().toISOString();
const readJsonFile = f => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return null; } };

let USERS = [];
let JOBS = [];

/* セッション署名キー。
   再起動で変わるとログインし直しになるだけなので、環境変数があればそれを使い、
   無ければ都度生成する（ローカル開発時はファイルに保存して使い回す）。 */
const SECRET = (() => {
  if (process.env.SESSION_SECRET) return process.env.SESSION_SECRET;
  const f = path.join(WORK_DIR, 'secret.key');
  if (!fs.existsSync(f)) fs.writeFileSync(f, crypto.randomBytes(32).toString('hex'), { mode: 0o600 });
  return fs.readFileSync(f, 'utf8').trim();
})();

/* 紛らわしい文字（0/O, 1/l/I）を除いた発行用パスワード。 */
function newPassword(len = 12) {
  const cs = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  return Array.from(crypto.randomBytes(len), b => cs[b % cs.length]).join('');
}
const saveUsers = () => DB.putUsers(USERS).catch(e => console.error('users保存失敗:', e.message));
const saveJob = j => DB.putJob(j).catch(e => console.error('job保存失敗:', e.message));

/* ---- 認証（HMAC署名Cookie） ---- */
const sign = s => crypto.createHmac('sha256', SECRET).update(s).digest('base64url');
function makeToken(u) {
  const body = Buffer.from(JSON.stringify({
    id: u.id, name: u.name, role: u.role, exp: Date.now() + 7 * 864e5,
  })).toString('base64url');
  return body + '.' + sign(body);
}
function readToken(req) {
  const m = /(?:^|;\s*)sid=([^;]+)/.exec(req.headers.cookie || '');
  if (!m) return null;
  const [body, sig] = m[1].split('.');
  if (!body || !sig || sign(body) !== sig) return null;
  let t;
  try { t = JSON.parse(Buffer.from(body, 'base64url').toString()); } catch { return null; }
  if (!(t.exp > Date.now())) return null;
  // Cookieは7日有効なので、無効化した利用者が締め出されるよう毎回実体を見る。
  const u = USERS.find(x => x.id === t.id);
  return u && !u.disabled ? { id: u.id, name: u.name, role: u.role } : null;
}

/* ---- HTTP小物 ---- */
function send(res, code, body, type = 'text/html; charset=utf-8', headers = {}) {
  res.writeHead(code, { 'Content-Type': type, ...headers });
  res.end(body);
}
const sendJson = (res, code, obj) => send(res, code, JSON.stringify(obj), 'application/json; charset=utf-8');
function redirect(res, loc, cookie) {
  const h = { Location: loc };
  if (cookie) h['Set-Cookie'] = cookie;
  res.writeHead(302, h); res.end();
}
function readBody(req, limit = 1e6) {
  return new Promise(r => {
    let b = '';
    req.on('data', c => { b += c; if (b.length > limit) req.destroy(); });
    req.on('end', () => r(b));
  });
}
const view = (file, repl = {}) => {
  let s = fs.readFileSync(VIEWS(file), 'utf8');
  for (const [k, v] of Object.entries(repl)) s = s.replace(k, () => v);
  return s;
};

/* ---- HRコード ----
   出力ファイルの名前と表の主キーになる。ここが崩れると格納先を間違えるので、
   ファイル名任せにせず画面で確定させる。 */
const HR_CODE_RE = /^\d{2}-[A-Z]{2}-[a-z]{2}-\d{2}$/;

/* =========================================================
   ジョブ実行（同時1本）
   ========================================================= */
let running = false;

function pump() {
  if (running) return;
  const job = JOBS.find(j => j.status === 'queued');
  if (!job) return;
  running = true;
  job.status = 'running';
  job.startedAt = now();
  job.log = [];
  saveJob(job);

  const outDir = path.join(OUT_DIR, job.id);
  fs.mkdirSync(outDir, { recursive: true });

  const p = spawn(PYTHON, [
    path.join(PROJECT_ROOT, 'tools', 'digest_poc.py'),
    '--video', job.videoPath,
    '--num-candidates', String(job.candidates),
    '--out-dir', outDir,
  ], {
    cwd: PROJECT_ROOT,
    // バッファリングされると進捗が最後にまとめて出て、画面が固まって見える。
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
  });

  // ログは進捗表示のためだけのもの。1行ごとに保存すると通信が膨れるので
  // メモリにだけ持ち、状態が変わったときにまとめて書く。
  //
  // ただしサーバのコンソールにも流す。ここを黙らせていたせいで、失敗したとき
  // Renderのログに何も残らず、原因を追うのに画面を開いたままにする必要があった。
  const onData = buf => {
    for (const line of String(buf).split('\n')) {
      const t = line.trimEnd();
      if (!t) continue;
      job.log.push(t);
      if (job.log.length > 400) job.log.shift();
      console.log(`[${job.id}] ${t}`);
    }
  };
  p.stdout.on('data', onData);
  p.stderr.on('data', onData);

  p.on('error', err => finish(job, 'error', `${PYTHON} を起動できませんでした: ${err.message}`));
  p.on('close', code => {
    if (job.status !== 'running') return; // error で確定済み
    if (code !== 0) return finish(job, 'error', `処理が異常終了しました（exit ${code}）`);
    try { collect(job, outDir); } catch (e) { return finish(job, 'error', e.message); }
    finish(job, 'done', null);
  });
}

/* 出力を回収する。一覧表もmdも十数KBしかないので、ファイルとして持たず
   ジョブの記録に中身ごと入れる。こうしておけば、再起動でディスクが
   まるごと消えても過去の出力を落とし直せる。
   xlsxはバイナリなのでbase64にする。1本あたり20KB程度で、
   テキストのまま持つより嵩むが、扱いを分けるほどの差ではない。 */
function collect(job, outDir) {
  const files = fs.readdirSync(outDir);
  const xlsx = files.find(f => f.endsWith('_digest_scenes.xlsx'));
  if (!xlsx) throw new Error('一覧表（xlsx）が生成されませんでした');
  job.xlsxB64 = fs.readFileSync(path.join(outDir, xlsx)).toString('base64');
  const md = files.find(f => f.endsWith('_digest_plan.md'));
  if (md) job.mdText = fs.readFileSync(path.join(outDir, md), 'utf8');

  const plan = readJsonFile(path.join(outDir, xlsx.replace('_digest_scenes.xlsx', '_digest_plan.json')));
  if (plan) {
    job.sceneCount = (plan.scenes || []).length;
    job.warnCount = (plan.warnings || []).length;
    job.videoDurationSec = plan.video_duration_sec;
    const pr = plan.params || {};
    job.costUsd = Number(((pr.select_cost_usd || 0) + (pr.transcribe_cost_usd || 0)).toFixed(6));
  }
  fs.rmSync(outDir, { recursive: true, force: true });
}

function finish(job, status, error) {
  job.status = status;
  job.error = error;
  job.finishedAt = now();
  /* 失敗したときだけ、ログの末尾を記録に残す。
     全文を残すと通信が膨れるが、原因は例外の直前数行にほぼ収まる。
     ここを残さないと、実行中の画面を閉じた時点で原因が追えなくなる。 */
  if (status === 'error') job.errorTail = (job.log || []).slice(-15).join('\n') || null;
  // 数百MBの動画を残すとディスクがすぐ埋まる。出力は記録側に移してあるので消してよい。
  try { if (job.videoPath) fs.rmSync(path.dirname(job.videoPath), { recursive: true, force: true }); } catch {}
  job.videoPath = null;
  saveJob(job);
  running = false;
  setImmediate(pump);
}

const publicJob = j => ({
  id: j.id, code: j.code, fileName: j.fileName, user: j.user, userName: j.userName,
  candidates: j.candidates, status: j.status, error: j.error || null,
  errorTail: j.errorTail || null,
  createdAt: j.createdAt, finishedAt: j.finishedAt || null,
  sceneCount: j.sceneCount || null, warnCount: j.warnCount || null,
  videoDurationSec: j.videoDurationSec || null, costUsd: j.costUsd || 0,
  // csvText は xlsx に切り替える前のジョブが持っている。過去分も落とせるようにしておく。
  hasSheet: !!j.xlsxB64, hasCsv: !!j.csvText, hasMd: !!j.mdText,
});

/* =========================================================
   ルーティング
   ========================================================= */
const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, 'http://x');
  const p = u.pathname;

  // 無料プランは無操作で停止するため、外形監視から叩ける口を開けておく。
  if (p === '/healthz') return send(res, 200, 'ok', 'text/plain');

  /* --- 認証不要 --- */
  if (p === '/login') {
    if (req.method === 'POST') {
      const b = qs.parse(await readBody(req));
      const usr = USERS.find(x => x.id === b.id && !x.disabled && x.pw === sha(b.pw || ''));
      if (usr) {
        usr.lastLoginAt = now(); saveUsers();
        return redirect(res, '/', `sid=${makeToken(usr)}; HttpOnly; Path=/; Max-Age=604800; SameSite=Lax${COOKIE_SUFFIX}`);
      }
      return send(res, 401, view('login.html', { __ERROR__: '<div class="err">IDまたはパスワードが違います</div>' }));
    }
    return send(res, 200, view('login.html', { __ERROR__: '' }));
  }
  if (p === '/logout') return redirect(res, '/login', 'sid=; Path=/; Max-Age=0');

  /* --- ここから要ログイン --- */
  const user = readToken(req);
  if (!user) {
    if (p.startsWith('/api/')) return sendJson(res, 401, { error: 'unauthorized' });
    return redirect(res, '/login');
  }
  const isAdmin = user.role === 'admin';

  if (p === '/') return send(res, 200, view('app.html', { __USER__: JSON.stringify(user) }));
  if (p === '/admin') {
    if (!isAdmin) return send(res, 403, '<p style="font-family:sans-serif;padding:40px">管理者権限が必要です。<a href="/">戻る</a></p>');
    return send(res, 200, view('admin.html', { __USER__: JSON.stringify(user) }));
  }

  /* --- アップロード ---
     multipartは自前で解くと数百MBのストリーム処理を誤りやすい。
     PUTで本体をそのまま流し、メタ情報はクエリで受ける。 */
  if (p === '/api/upload' && req.method === 'PUT') {
    const code = String(u.searchParams.get('code') || '').trim();
    if (!HR_CODE_RE.test(code)) return sendJson(res, 400, { error: 'HRコードの形式が不正です' });
    /* 音声だけでも受ける。パイプラインは冒頭で音声を抜いた後、映像を一切見ない。
       30分のHRは動画だと1〜2GBあるが、書き出した音声なら数十MBで済む。
       アップロードが待ち時間の大半なので、編集ソフトから音声だけ書き出せる
       場合はそのほうが速い。ffmpegは中身を見て判別するので扱いは変わらない。 */
    const orig = path.basename(String(u.searchParams.get('name') || 'video.mp4'));
    const ext = ['.mp4', '.mov', '.m4v', '.mkv',
      '.m4a', '.mp3', '.wav', '.aac', '.flac', '.ogg'].includes(path.extname(orig).toLowerCase())
      ? path.extname(orig).toLowerCase() : '.mp4';
    // digest_poc.py は動画のstem（拡張子なしのファイル名）を出力物の名前や
    // md の見出しに使う。ファイル名をHRコードそのものにしておかないと
    // 「25-UT-ot-01」ではなく uuid が表に出てしまう。衝突は親ディレクトリで避ける。
    const id = crypto.randomUUID();
    const dir = path.join(UPLOAD_DIR, id);
    fs.mkdirSync(dir, { recursive: true });
    const dest = path.join(dir, `${code}${ext}`);
    const ws = fs.createWriteStream(dest);
    let bytes = 0, aborted = false;
    const scrap = () => { try { fs.rmSync(dir, { recursive: true, force: true }); } catch {} };
    req.on('data', c => {
      bytes += c.length;
      if (bytes > MAX_UPLOAD_BYTES && !aborted) { aborted = true; req.destroy(); }
    });
    req.pipe(ws);
    ws.on('error', () => { scrap(); if (!res.headersSent) sendJson(res, 500, { error: '保存に失敗しました' }); });
    req.on('aborted', scrap);
    ws.on('close', () => {
      if (aborted || bytes === 0) return scrap();
      sendJson(res, 200, { uploadId: id, fileName: orig, bytes });
    });
    return;
  }

  if (p === '/api/jobs' && req.method === 'POST') {
    let b; try { b = JSON.parse(await readBody(req) || '{}'); } catch { return sendJson(res, 400, { error: 'bad json' }); }
    const code = String(b.code || '').trim();
    if (!HR_CODE_RE.test(code)) return sendJson(res, 400, { error: 'HRコードの形式が不正です' });
    const uploadId = String(b.uploadId || '');
    if (!/^[0-9a-f-]{36}$/.test(uploadId)) return sendJson(res, 400, { error: 'アップロードIDが不正です' });
    const dir = path.join(UPLOAD_DIR, uploadId);
    const entry = fs.existsSync(dir) ? fs.readdirSync(dir)[0] : null;
    if (!entry) return sendJson(res, 400, { error: 'アップロードが見つかりません' });
    const job = {
      id: crypto.randomUUID().slice(0, 8), code, fileName: String(b.fileName || entry),
      user: user.id, userName: user.name,
      candidates: Math.min(12, Math.max(3, parseInt(b.candidates, 10) || 6)),
      videoPath: path.join(dir, entry),
      status: 'queued', createdAt: now(), log: [],
    };
    JOBS.unshift(job); saveJob(job); pump();
    return sendJson(res, 200, { job: publicJob(job) });
  }

  if (p === '/api/jobs' && req.method === 'GET') {
    const list = (isAdmin ? JOBS : JOBS.filter(j => j.user === user.id)).slice(0, 100);
    return sendJson(res, 200, { jobs: list.map(publicJob) });
  }

  /* ダウンロード。ファイル名はHRコード始まりで固定する（格納先を間違えないため）。
     csv は xlsx に切り替える前のジョブのために残してある。 */
  const DL = {
    '/xlsx': j => j.xlsxB64 && [Buffer.from(j.xlsxB64, 'base64'),
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'scenes.xlsx'],
    '/csv': j => j.csvText && [j.csvText, 'text/csv; charset=utf-8', 'scenes.csv'],
    '/md': j => j.mdText && [j.mdText, 'text/markdown; charset=utf-8', 'plan.md'],
  };
  const jm = /^\/api\/jobs\/([\w-]+)(\/xlsx|\/csv|\/md)?$/.exec(p);
  if (jm) {
    const job = JOBS.find(j => j.id === jm[1]);
    if (!job) return sendJson(res, 404, { error: 'not found' });
    if (!isAdmin && job.user !== user.id) return sendJson(res, 403, { error: 'forbidden' });
    if (jm[2]) {
      const got = DL[jm[2]](job);
      if (!got) return sendJson(res, 404, { error: 'not found' });
      const [body, type, suffix] = got;
      return send(res, 200, body, type,
        { 'Content-Disposition': `attachment; filename="${job.code}_digest_${suffix}"` });
    }
    return sendJson(res, 200, { job: publicJob(job), log: job.log || [] });
  }

  /* --- 自分のパスワード変更 --- */
  if (p === '/api/password' && req.method === 'POST') {
    let b; try { b = JSON.parse(await readBody(req) || '{}'); } catch { return sendJson(res, 400, { error: 'bad json' }); }
    const me = USERS.find(x => x.id === user.id);
    if (!me || me.pw !== sha(b.current || '')) return sendJson(res, 400, { error: '現在のパスワードが違います' });
    if (String(b.next || '').length < 8) return sendJson(res, 400, { error: '新しいパスワードは8文字以上にしてください' });
    me.pw = sha(b.next); saveUsers();
    return sendJson(res, 200, { ok: true });
  }

  /* --- 管理者API --- */
  if (p.startsWith('/api/users')) {
    if (!isAdmin) return sendJson(res, 403, { error: 'forbidden' });
    if (p === '/api/users' && req.method === 'GET') {
      return sendJson(res, 200, {
        users: USERS.map(x => ({
          id: x.id, name: x.name, role: x.role, disabled: !!x.disabled,
          createdAt: x.createdAt || null, lastLoginAt: x.lastLoginAt || null,
          jobs: JOBS.filter(j => j.user === x.id).length,
        })),
      });
    }
    if (p === '/api/users' && req.method === 'POST') {
      let b; try { b = JSON.parse(await readBody(req) || '{}'); } catch { return sendJson(res, 400, { error: 'bad json' }); }
      const id = String(b.id || '').trim();
      if (!/^[a-zA-Z0-9._-]{3,32}$/.test(id)) return sendJson(res, 400, { error: 'IDは英数字3〜32文字にしてください' });
      if (USERS.some(x => x.id === id)) return sendJson(res, 400, { error: 'そのIDは既に存在します' });
      const pw = newPassword();
      USERS.push({ id, name: String(b.name || id).slice(0, 40), role: 'editor', pw: sha(pw), disabled: false, createdAt: now() });
      saveUsers();
      // 平文はここでしか返さない（保存もしない）。控え忘れたら再発行する。
      return sendJson(res, 200, { ok: true, id, password: pw });
    }
    const um = /^\/api\/users\/([\w.-]+)\/(toggle|reset)$/.exec(p);
    if (um && req.method === 'POST') {
      const target = USERS.find(x => x.id === um[1]);
      if (!target) return sendJson(res, 404, { error: 'not found' });
      if (target.id === user.id) return sendJson(res, 400, { error: '自分自身は操作できません' });
      if (um[2] === 'toggle') {
        target.disabled = !target.disabled; saveUsers();
        return sendJson(res, 200, { ok: true, disabled: target.disabled });
      }
      const pw = newPassword();
      target.pw = sha(pw); saveUsers();
      return sendJson(res, 200, { ok: true, password: pw });
    }
  }

  send(res, 404, 'not found', 'text/plain; charset=utf-8');
});

/* =========================================================
   起動
   ========================================================= */
(async () => {
  const state = await DB.load();
  USERS = state.users || [];
  JOBS = state.jobs || [];

  /* 初回だけ管理者を1つ作る。パスワードはここでしか表示しない。 */
  if (!USERS.length) {
    const pw = process.env.ADMIN_PASSWORD || newPassword();
    USERS = [{ id: 'admin', name: '管理者', role: 'admin', pw: sha(pw), disabled: false, createdAt: now() }];
    await DB.putUsers(USERS);
    console.log('\n' + '='.repeat(58));
    console.log('  管理者アカウントを作成しました（初回のみ表示）');
    console.log('    ID       : admin');
    console.log('    パスワード : ' + pw);
    console.log('='.repeat(58) + '\n');
  }

  /* 前回の残骸を捨てる。uploads と outputs は使い捨てで、起動時点で
     処理中のジョブは存在しない。異常終了で数百MBの動画が残っていると
     ディスクが埋まるので、ここで確実に空にしておく。 */
  for (const d of [UPLOAD_DIR, OUT_DIR]) {
    try {
      for (const n of fs.readdirSync(d)) fs.rmSync(path.join(d, n), { recursive: true, force: true });
    } catch {}
  }

  /* 前回の停止・再デプロイで running のまま残ったジョブを回収する。
     無料プランは無操作で停止するので、これは日常的に起きる。 */
  for (const j of JOBS) {
    if (j.status === 'running' || j.status === 'queued') {
      j.status = 'error';
      j.error = 'サーバの再起動により中断されました。もう一度実行してください';
      j.finishedAt = now();
      saveJob(j);
    }
  }

  server.listen(PORT, () => {
    console.log(`4HR ダイジェスト抽出アプリ: http://localhost:${PORT}`);
    console.log(`  状態の保存先: ${DB.kind}`);
    console.log(`  作業ディレクトリ: ${WORK_DIR}`);
  });
})().catch(e => { console.error('起動に失敗しました:', e); process.exit(1); });
