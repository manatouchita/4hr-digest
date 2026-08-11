# =========================================================
# 4HR ダイジェスト抽出アプリ
#
# Nodeだけでは動かない。処理の本体は Python（tools/digest_poc.py）で、
# さらに音声抽出に ffmpeg が要る。Renderの標準Node環境には
# どちらも入っていないので、Dockerで揃える。
# =========================================================
FROM node:20-slim

# ffmpeg  : 動画から音声を抜く（ffprobe も同じパッケージに入っている）
# python3 : 見どころ選定パイプライン本体
#
# apt のキャッシュは残すとイメージが無駄に大きくなるので同じ層で消す。
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Debianの python3 にそのまま pip すると PEP668 で弾かれる。
# システムを汚さないよう仮想環境に入れ、PYTHON でそこを指す。
# anthropic : 見どころ選定
# openpyxl  : 編集者に渡す一覧表（xlsx）の書き出し
ENV VENV=/opt/venv
RUN python3 -m venv $VENV && $VENV/bin/pip install --no-cache-dir anthropic openpyxl
ENV PYTHON=$VENV/bin/python

WORKDIR /app

# npm install は無い。アプリはNode標準ライブラリだけで動く。
# 依存を足したくなったら、まずそれを標準ライブラリで書けないか考える
# （この構成の一番の利点は、壊れる箇所が少ないこと）。
COPY tools/ ./tools/
COPY digest_app/ ./digest_app/

# 動画と中間ファイルの置き場。再起動で消えて構わない（消えても再実行すれば済む）。
ENV DIGEST_WORK_DIR=/tmp/digest
ENV NODE_ENV=production
EXPOSE 3100

CMD ["node", "digest_app/app.js"]
