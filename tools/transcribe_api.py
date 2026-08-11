#!/usr/bin/env python3
"""OpenAI Whisper APIでHR動画を文字起こしし、
digest_poc.py 互換のキャッシュJSONを digest_cache/ に書き出す。

faster-whisper のインストールやモデルDLは不要。必要なのは ffmpeg と APIキーのみ。
キーは環境変数 OPENAI_API_KEY またはプロジェクト直下 .env の OPENAI_API_KEY= から読む。

使い方:
    python3 tools/transcribe_api.py --video "2025年度映像（参考）/2025/25-TT-ph-01-HR.mp4"

その後の見どころ選定は digest_poc.py 側でキャッシュが自動利用される:
    python3 tools/digest_poc.py --video <同じ動画> --whisper-model api-whisper --scenes-file <scenes.json>
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "digest_cache"


def _clean_key(v: str) -> str:
    """APIキーから空白を全部落とす。

    キーの途中に空白が入ることは正当には有り得ない。一方で、長いキーを
    設定画面やターミナルから貼ると途中で折り返されて改行が紛れ込むことがあり、
    そのままHTTPヘッダに載せると `Illegal header value` で落ちる。
    エラーはヘッダ組み立ての段で出るため原因が分かりにくく、しかも
    文字起こしを終えた後の工程で落ちると費用だけ掛かって成果が残らない。
    前後だけでなく内部の空白も落として、貼り方の事故を吸収する。
    """
    return re.sub(r"\s+", "", v or "")


def load_env_key(name: str) -> str | None:
    """環境変数 → プロジェクト直下の .env の順で探す。"""
    if os.environ.get(name):
        return _clean_key(os.environ[name]) or None
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return _clean_key(line.split("=", 1)[1].strip().strip('"').strip("'")) or None
    return None
API_URL = "https://api.openai.com/v1/audio/transcriptions"
MODEL_NAME = "api-whisper"  # digest_poc.py の --whisper-model に渡す名前
MAX_UPLOAD_MB = 24  # OpenAI の上限は 25MB。余裕を見て 24MB で停止する

# 誤変換が実測で確認された語を中心にした既定の用語リスト。
# 例: 「京大」→「兄弟」「巨大」、「得点力」→「特典力」、「近似計算」→「臨時計算」
DEFAULT_VOCAB = [
    "京大", "京都大学", "東大", "東京大学", "一橋大学", "東京科学大学",
    "東進", "共通テスト", "二次試験", "過去問演習", "問題演習",
    "大問", "設問", "配点", "得点力", "目標得点", "時間配分",
    "力学", "電磁気", "波動", "熱力学", "原子", "近似計算", "有効数字",
    "力学的エネルギー", "運動方程式", "相対運動", "万有引力",
    "数学", "英語", "物理", "化学", "生物", "地学", "国語", "日本史", "世界史",
]


def extract_audio_mp3(video: Path, out_mp3: Path) -> Path:
    """アップロードサイズ削減のため 16kHz モノラル mp3 に変換。"""
    cmd = ["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1",
           "-ar", "16000", "-b:a", "64k", str(out_mp3)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 音声抽出に失敗:\n{r.stderr[-800:]}")
    size_mb = out_mp3.stat().st_size / 1024 / 1024
    if size_mb > MAX_UPLOAD_MB:
        raise RuntimeError(
            f"mp3 が {size_mb:.1f}MB で OpenAI の 25MB 上限に近すぎます "
            f"({MAX_UPLOAD_MB}MB 超で停止)。長尺動画の分割対応は未実装です。\n"
            f"  対象: {out_mp3}"
        )
    print(f"  mp3: {size_mb:.1f}MB")
    return out_mp3


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float((r.stdout or "0").strip() or 0)


def multipart_body(fields: dict, file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    lines: list[bytes] = []
    for k, v in fields.items():
        # 同じ名前を複数回送る必要のあるフィールド（timestamp_granularities[] など）
        # はリストで渡す。
        for item in (v if isinstance(v, list) else [v]):
            lines += [f"--{boundary}".encode(),
                      f'Content-Disposition: form-data; name="{k}"'.encode(),
                      b"", str(item).encode()]
    ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    lines += [f"--{boundary}".encode(),
              (f'Content-Disposition: form-data; name="{file_field}"; '
               f'filename="{file_path.name}"').encode(),
              f"Content-Type: {ctype}".encode(), b"",
              file_path.read_bytes(),
              f"--{boundary}--".encode(), b""]
    return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"


def load_vocab_prompt() -> str:
    """固有名詞の誤変換を抑えるための用語リストを組み立てる。

    Whisper の prompt は「直前の文脈」として扱われるため、期待する表記を
    自然文で並べておくと同音異義語がその表記に寄る。
    configs/digest.local.json の "vocab" があれば追記できる。
    """
    terms = list(DEFAULT_VOCAB)
    cfg = PROJECT_ROOT / "configs" / "digest.local.json"
    if cfg.exists():
        try:
            extra = json.loads(cfg.read_text(encoding="utf-8")).get("vocab") or []
            terms += [t for t in extra if t not in terms]
        except Exception as e:
            print(f"  警告: {cfg.name} を読めませんでした: {e}")
    return "以下は大学受験の解説です。用語: " + "、".join(terms) + "。"


def call_api(audio: Path, api_key: str, language: str,
             prompt: str | None = None) -> dict:
    fields = {"model": "whisper-1", "language": language,
              "response_format": "verbose_json",
              # 語単位の時刻。APIの既定セグメントは動画によって中央値4秒〜11秒と
              # ばらつきがあり、11秒粒度では「20〜35秒のシーン」を段に合わせて
              # 切り出せない。語単位から自前で細かいセグメントを組み直す。
              "timestamp_granularities[]": ["word", "segment"]}
    if prompt:
        fields["prompt"] = prompt
    body, ctype = multipart_body(fields, "file", audio)
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        if e.code == 401:
            raise SystemExit(
                "エラー: APIキーが拒否されました(401)。\n"
                "  platform.openai.com でキー(sk-…)の有効性・残クレジットを確認し、\n"
                f"  {PROJECT_ROOT / '.env'} の OPENAI_API_KEY を更新してください。\n"
                f"  APIからの詳細: {detail}")
        raise SystemExit(f"エラー: APIエラー {e.code}\n  詳細: {detail}")


# 自前セグメントの設計値。切り出し精度を上げるため既定セグメントより細かくする。
SEG_MAX_SEC = 6.0    # 1セグメントの上限
SEG_MIN_SEC = 1.5    # これ未満なら区切らず前に足す
PAUSE_SEC = 0.30     # これ以上の間があれば文の切れ目とみなす
SILENCE_SEC = 2.0    # これ以上の間は「考える時間・メモ時間」。必ず切り離す


def _split_long(chunk: list[dict]) -> list[list[dict]]:
    """長すぎる語の並びを、その中で最も大きい「間」で二分割する（再帰）。

    秒数だけを見て機械的に切ると語の途中で切れ、切り出した映像が
    「〜ポンって上がるかもし」のような尻切れになる。実際に間が空いた
    ところで切れば、その危険はほぼ無くなる。
    """
    span = chunk[-1]["end"] - chunk[0]["start"]
    if span <= SEG_MAX_SEC or len(chunk) < 2:
        return [chunk]
    # 分割後の両側が最小長を満たす位置の中で、最も間が大きいところを選ぶ。
    best_i, best_gap = None, -1.0
    for i in range(1, len(chunk)):
        if (chunk[i - 1]["end"] - chunk[0]["start"] < SEG_MIN_SEC
                or chunk[-1]["end"] - chunk[i]["start"] < SEG_MIN_SEC):
            continue
        gap = chunk[i]["start"] - chunk[i - 1]["end"]
        if gap > best_gap:
            best_i, best_gap = i, gap
    if best_i is None:
        return [chunk]
    return _split_long(chunk[:best_i]) + _split_long(chunk[best_i:])


def segments_from_words(words: list[dict]) -> list[dict]:
    """語単位の時刻から、区切りの良い細かいセグメントを組み直す。

    まず「間（ポーズ）」で分け、それでも長すぎる塊だけを最大の間で割る。
    日本語は語間に空白を入れないため、語は素朴に連結する。
    """
    ws_list = []
    for w in words:
        text = (w.get("word") or "").strip()
        if text:
            ws_list.append({"start": float(w.get("start", 0)),
                            "end": float(w.get("end", 0)), "text": text})
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    for w in ws_list:
        gap = w["start"] - cur[-1]["end"] if cur else 0.0
        # 長い沈黙は最小長の制約より優先して切り離す。そうしないと沈黙が
        # セグメントの内側に隠れ、選定側から見えなくなる。
        if cur and (gap >= SILENCE_SEC
                    or (gap >= PAUSE_SEC
                        and cur[-1]["end"] - cur[0]["start"] >= SEG_MIN_SEC)):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)

    segs: list[dict] = []
    for c in chunks:
        segs.extend(_split_long(c))
    return [{"idx": i, "start": round(c[0]["start"], 2),
             "end": round(c[-1]["end"], 2),
             "text": "".join(w["text"] for w in c)}
            for i, c in enumerate(segs)]


def to_cache_format(api_result: dict, duration_fallback: float) -> dict:
    words = api_result.get("words") or []
    segs = segments_from_words(words) if words else []
    if not segs:
        # 語単位が返らなかった場合は API 既定のセグメントで代替する。
        for i, s in enumerate(api_result.get("segments") or []):
            text = (s.get("text") or "").strip()
            if not text:
                continue
            segs.append({"idx": i, "start": round(float(s["start"]), 2),
                         "end": round(float(s["end"]), 2), "text": text})
    duration = float(api_result.get("duration") or 0) or duration_fallback \
        or (segs[-1]["end"] if segs else 0)
    return {"model": MODEL_NAME,
            "language": api_result.get("language", "ja"),
            "duration": round(duration, 2), "segments": segs}


def cache_path(video: Path) -> Path:
    st = video.stat()
    key = f"{video.stem}.{MODEL_NAME}.{int(st.st_size)}-{int(st.st_mtime)}"
    return CACHE_DIR / f"{key}.json"


def transcribe_video(video: Path, *, language: str = "ja",
                     api_key: str | None = None, use_vocab: bool = True,
                     log=print) -> dict:
    """動画1本を文字起こしし、digest_poc が読むキャッシュ形式の dict を返す。

    以前は「まずこのCLIを叩いてキャッシュを作り、次に digest_poc を叩く」
    という2段階運用だった。人が手で回す分には成立するが、Webアプリから
    2プロセス起動すると失敗箇所の切り分けが面倒になるため、関数として
    呼べる形にしておく。
    """
    api_key = api_key or load_env_key("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY が見つかりません"
                           "（環境変数か .env に設定してください）")
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fh:
        mp3 = Path(fh.name)
    try:
        log("    音声抽出中…")
        extract_audio_mp3(video, mp3)
        prompt = load_vocab_prompt() if use_vocab else None
        log(f"    APIへ送信中…（{mp3.stat().st_size / 1e6:.1f} MB"
            + ("" if prompt is None else " / 用語リストあり") + "）")
        result = call_api(mp3, api_key, language, prompt)
        return to_cache_format(result, probe_duration(video))
    finally:
        mp3.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--language", default="ja")
    ap.add_argument("--key", default=None, help="APIキー(省略時は環境変数か .env の OPENAI_API_KEY)")
    ap.add_argument("--no-vocab", action="store_true",
                    help="固有名詞の用語リストを渡さない(比較検証用)")
    ap.add_argument("--force", action="store_true", help="キャッシュがあっても再実行")
    args = ap.parse_args()

    api_key = args.key or load_env_key("OPENAI_API_KEY")
    if not api_key:
        print("エラー: --key か OPENAI_API_KEY(環境変数 または .env)でAPIキーを指定してください")
        return 1

    video = Path(args.video)
    if not video.exists():
        video = PROJECT_ROOT / args.video
    if not video.exists():
        print(f"エラー: 動画が見つかりません: {args.video}")
        return 1

    cp = cache_path(video)
    if cp.exists() and not args.force:
        print(f"既にキャッシュあり: {cp.name}(再実行不要)")
        return 0

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = transcribe_video(video, language=args.language, api_key=api_key,
                             use_vocab=not args.no_vocab)
    cp.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    print(f"完了: {cp.relative_to(PROJECT_ROOT)}（セグメント{len(cache['segments'])}件, "
          f"{cache['duration']:.0f}秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
