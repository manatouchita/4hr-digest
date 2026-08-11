# デプロイ手順（初回のみ・所要40分ほど）

編集者が各自のブラウザから使えるようにする。月額は0円（Render無料 + Supabase無料）。
上から順にやれば終わる。**手順3までは待ち時間が無いので、先に3つとも済ませてよい。**

---

## 1. Supabase（状態の保存先）

Renderの無料プランは再起動でファイルが消える。消えて困るもの
（アカウント・実行履歴・出力したCSV）だけをここに置く。全部あわせて数百KB。

1. https://supabase.com にGitHubアカウントでサインイン
2. **New project**
   - Name: `4hr-digest`
   - Database Password: 自動生成のまま（**この後使わない**。控えなくてよい）
   - Region: `Northeast Asia (Tokyo)`
3. 作成完了まで2分ほど待つ
4. 左メニュー **SQL Editor** → `digest_app/schema.sql` の中身を全部貼って **Run**
   → `Success. No rows returned` と出れば成功
5. 左下の歯車 **Project Settings** → **API** で次の2つを控える
   - **Project URL**（`https://xxxx.supabase.co`）→ 手順3の `SUPABASE_URL`
   - **service_role** の `secret` キー → 手順3の `SUPABASE_SERVICE_KEY`

> `anon public` ではなく **service_role** のほう。
> このキーは全データを読み書きできる。Renderの設定欄以外に貼らないこと。

---

## 2. GitHub（コードの置き場）

1. https://github.com/new
   - Repository name: `4hr-digest`
   - **Private** を選ぶ
   - README等のチェックは全部外す
2. このフォルダをそのまま上げる。
   **1行目の `cd` を必ず実行すること。** ホームディレクトリで git を叩くと、
   ホーム配下の全ファイル（動画を含む）を取り込み始めてディスクを食い潰す。

```bash
cd ~/Documents/Claude/Projects/4HR-digest-deploy
git remote add origin https://github.com/manatouchita/4hr-digest.git
git push -u origin main
```

> `git init` とコミットは済んでいるので不要。
> `git push` が `Everything up-to-date` か `new branch main -> main` と出れば成功。

---

## 3. Render（アプリを動かす場所）

1. https://render.com にGitHubアカウントでサインイン
2. **New +** → **Blueprint** → さっきの `4hr-digest` リポジトリを選ぶ

   > **Web Service ではなく Blueprint を選ぶこと。**
   > Web Service で作るとRenderが `package.json` を見てNode環境と判定し、
   > `render.yaml` が無視される。するとffmpegとPythonの入っていない
   > コンテナで動いてしまい、起動はしてもジョブが必ず失敗する。
   > Blueprint なら `render.yaml` が読まれ、Dockerでビルドされる。

3. 設定は `render.yaml` に書いてあるので触らなくてよい。
   ログに `==> Building image` のような行が出ればDockerで動いている。
   `yarn start` や `npm start` が出ていたら選択を間違えているので作り直す
4. **Environment Variables** に次を入れる（`SESSION_SECRET` は自動生成されるので不要）

   | キー | 値 |
   |------|-----|
   | `SUPABASE_URL` | 手順1で控えたProject URL（`https://xxxx.supabase.co`。末尾に `/rest/v1` を付けない） |
   | `SUPABASE_SERVICE_KEY` | 手順1で控えたservice_roleキー |
   | `OPENAI_API_KEY` | 文字起こし用（既存のもの） |
   | `ANTHROPIC_API_KEY` | 見どころ選定用（既存のもの） |
   | `ADMIN_PASSWORD` | 管理者の初期パスワード。**自分で決めて入れる** |

5. **Create Web Service** → 初回ビルドは10分ほどかかる（ffmpegとPythonを入れるため）
6. `https://4hr-digest.onrender.com` のようなURLが発行される

### うまくいかないとき

- **ビルドが失敗する** → Logsタブの赤い行を見る。ffmpegやpipの取得失敗なら、
  時間を置いて **Manual Deploy → Clear build cache & deploy**
- **画面は出るがジョブが必ず失敗する** → ログに
  `ANTHROPIC_API_KEY が見つかりません` 等が出ていないか見る。環境変数の入れ忘れ。
  `ffmpeg: not found` や `python3: not found` なら手順2の選択ミス（Docker で動いていない）
- **`Supabase 404 ... PGRST125`** → `SUPABASE_URL` にパスが混ざっている。
  `https://xxxx.supabase.co` だけにする
- **`Supabase 404 ... PGRST205`（Could not find the table）** → 手順1のSQLを流し忘れ
- **`Supabase 401`** → キーが違う。`anon public` ではなく **service_role** のほう
- **ログインしても弾かれる** → `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` が違うと
  起動ログの「状態の保存先」が `file` になる。`supabase` と出ているか確認

---

## 4. 使い始める

1. 発行されたURLを開く → ID `admin` / 手順3で決めたパスワードでログイン
2. `/admin` を開いて編集者のアカウントを人数分発行する
   - **初期パスワードはその場で1回しか出ない**。コピーして本人に渡す
   - 控え忘れたら、同じ画面の「PW再発行」を押せばよい
3. 編集者に伝えること
   - URLとID・パスワード
   - **完成版（カット済み）の動画を1本アップロードする**こと
   - 出てきたCSVはDriveに手で格納すること
   - **抜粋文は自動文字起こしなので固有名詞に誤変換がある**。
     切り出す位置の目印として使い、内容は必ず映像で確認すること

---

## 運用で知っておくこと

### 15分使わないと寝る（無料プランの仕様）

次に開いた人は**起動に1分ほど待たされる**。壊れているわけではない。
気になるならUptimeRobot等で `https://<URL>/healthz` を5分おきに叩けば起きたままにできるが、
無料枠の稼働時間（月750時間）を使い切る点に注意。使う時間帯だけ叩くのが無難。

### 寝るときに実行中だったジョブは失われる

次回起動時に「サーバの再起動により中断されました」と表示される。もう一度実行すればよい。
処理は数分で終わるので、アップロードしたら画面を閉じずに待つのが安全。

### 費用

APIの実費だけ。**1本あたり30〜50円**（30分の動画）。
`/admin` に今月の合計が出るので、たまに見ておく。
Render・Supabaseは無料枠の範囲。

### 制約

- **52分を超える動画は通らない**（文字起こしAPIの上限）
- 同時に処理できるのは1本。2本目は待機列に入る
- 同じ動画を上げ直すと文字起こしからやり直しで、費用が二重にかかる

---

## コードを直したあと

このリポジトリは4HRプロジェクトからの**写し**。直すのは常に本体のほう。

```bash
# 4HRプロジェクト側で
bash digest_app/sync-deploy.sh
cd ../4HR-digest-deploy
git add -A && git commit -m "何を直したか" && git push
```

pushするとRenderが自動で再ビルドする（5〜10分）。
このフォルダを直接編集すると、次の同期で消える。
