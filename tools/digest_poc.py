#!/usr/bin/env python3
"""4HRダイジェスト見どころ選定 PoC（単一ファイル・検証用）。

過去HR1本で「実運用に耐えるか」を確かめるための最小パイプライン:
  音声抽出(ffmpeg) → 文字起こし(faster-whisper) → 見どころ選定(Claude)
  → 時刻検証/スナップ → JSON+Markdown出力 → 人間ダイジェストとの突合

Usage:
  python tools/digest_poc.py \
    --video "2025年度映像（参考）/2025/25-KU-ph-01-HR.mp4" \
    --human-digest "2025年度映像（参考）/2025/25-KU-ph-01-digest.mp4" \
    --num-candidates 12 --whisper-model large-v3-turbo

依存: faster-whisper, anthropic, ffmpeg/ffprobe(PATH)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_OUT = PROJECT_ROOT / "digest_plans"
CACHE_DIR = PROJECT_ROOT / "digest_cache"
SELECT_MODEL = "claude-sonnet-4-6"
# 語単位の時刻が返るのはAPI版だけで、これが既定の経路。
# ローカルのfaster-whisperは検証用に残してある。
MODEL_API = "api-whisper"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# 突合時に無視する記号。Whisperの出力は句読点を持たず空白区切りのため、
# 読みやすさのために補われた句読点・括弧類で誤検出しないよう両側から除去する。
# 長音「ー」や中黒を含む固有名詞は保持する（文字の取り違えは検出したい）。
_PUNCT_RE = re.compile(r"[\s、。，．,.!！?？「」『』（）()\[\]｛｝{}…‥"
                       r"\"'\u201c\u201d\u2018\u2019]+")


MIN_SCENE_SEC = 20  # 1シーンの想定尺（下限）
MAX_SCENE_SEC = 35  # 1シーンの想定尺（上限）


def norm(s: str) -> str:
    """NFC正規化＋空白・句読点除去（突合用）。"""
    return _PUNCT_RE.sub("", unicodedata.normalize("NFC", s))


def fmt_hhmmss(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


# ── 音声抽出 ──────────────────────────────────────────
def extract_audio(video: Path, out_wav: Path) -> Path:
    cmd = ["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1",
           "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 音声抽出に失敗: {video}\n{r.stderr[-1000:]}")
    return out_wav


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float((r.stdout or "0").strip() or 0)


# ── 文字起こし ────────────────────────────────────────
def transcribe(wav: Path, *, model_size: str, language: str) -> dict:
    from faster_whisper import WhisperModel
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(wav), language=language, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    segs = []
    for i, s in enumerate(segments):
        text = s.text.strip()
        if not text:
            continue
        segs.append({"idx": i, "start": round(s.start, 2),
                     "end": round(s.end, 2), "text": text})
    return {"model": model_size, "language": info.language,
            "duration": round(info.duration, 2), "segments": segs}


def cache_path(video: Path, model_size: str) -> Path:
    st = video.stat()
    key = f"{video.stem}.{model_size}.{int(st.st_size)}-{int(st.st_mtime)}"
    return CACHE_DIR / f"{key}.json"


def load_or_transcribe(video: Path, *, model_size: str, language: str,
                       no_cache: bool) -> dict:
    cp = cache_path(video, model_size)
    if cp.exists() and not no_cache:
        print(f"    キャッシュ利用: {cp.name}")
        cached = json.loads(cp.read_text(encoding="utf-8"))
        # 再実行では文字起こし費用が発生しない。実行履歴に正しい額を出すため印を付ける。
        cached["from_cache"] = True
        return cached
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    t0 = datetime.now()
    if model_size == MODEL_API:
        # 語単位の時刻が要るのでこちらが本番経路。ローカルのfaster-whisperは
        # 語単位を返さず、20〜35秒のシーンを段に合わせて切り出せない。
        from transcribe_api import transcribe_video
        print("    文字起こし中…（OpenAI Whisper API）")
        result = transcribe_video(video, language=language)
    else:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            wav = Path(fh.name)
        try:
            print("    音声抽出中…")
            extract_audio(video, wav)
            print(f"    文字起こし中… (model={model_size}, CPU)")
            result = transcribe(wav, model_size=model_size, language=language)
        finally:
            wav.unlink(missing_ok=True)
    result["transcribe_seconds"] = (datetime.now() - t0).total_seconds()
    cp.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    return result


# ── 見どころ選定（Claude）─────────────────────────────
SYSTEM_PROMPT = """あなたは大学受験HR動画の広報ダイジェスト（予告編）編集者です。約15分の講義動画の文字起こしから、次回放映を「見たい」と思わせる予告編に入れる見どころシーンを確定します。

## これは予告編（teaser）である
視聴者は対象HRを未視聴・前提知識ゼロ。価値を与えきるのではなく「面白そう、続きが見たい」を喚起するのが目的。答えを言い切るシーンより、引きを残すシーンを優先する。

## 選んではいけないもの（概要説明は不要）
予告編の冒頭に置く「このHRは何の話か」という概要紹介は、**別途テンプレート映像で用意するのでAIは選ばない**。
以下は候補から除外する:
- 自己紹介（名前・学部・出身・サークル・大学生活の紹介）。
- 「本日の流れはこちらです」「まず〜について話します」といった目次・進行の予告。
- HR全体のテーマ説明や、試験の基本情報の羅列（配点・試験時間・出題分野の一覧など）。
- 「以上です」「ご清聴ありがとうございました」などの締め。
選ぶのは**中身そのもの**、つまり具体的な一手・体験談・受験生の心情に触れる部分だけである。

## 見どころ評価軸（重要度順）
1. 具体的な一手（最重視）: 今日から実践できる勉強法・解法・戦略。生徒満足度の最大ドライバ。
2. 感情・動機づけ: 時期に合った不安への共感、孤立感の解消、励まし、印象的な体験談。
3. フック: 冒頭で視聴価値が伝わる、続きが気になる、印象的な一言。

## シーンとは「それだけ流して話が通じる一続きの塊」である（最重要）
一文だけ切り出しても未視聴者には何の話か分からない。選ぶ単位は「刺さる一言」ではなく、**前提から核心までが揃った一続きの話**である。

各シーンは、この3つを1本の中に収める:
1. **前提**: 何の話が始まるのかが分かる導入（例「大問1の特徴ですが」「時間配分のポイントは」）。話の途中から始めない。
2. **核心**: そのシーンで一番効く主張・具体的な一手・共感の一言。
3. **引き**: 答えを言い切る手前で終える。

判定基準は「**この20〜35秒だけを何も知らない受験生に見せて、話の筋が通り、続きが気になるか**」。
以下は失格:
- 冒頭が「それができるのは」「そのため」「これによって」など、前の文を受ける語で始まっている（前提が欠けている）。
- 指示語（これ・それ・この2つ）の指す対象が範囲内に無い。
- 話題の途中でぶつ切りに終わり、何が言いたかったのか分からない。

**ネタバレの扱い**: 前提と問題提起は見せてよい。見せてはいけないのは「具体的なやり方の中身」「結論の答え」。
例: 「過去問だけでは伸びない。普段の演習と行き来することが大事」までは可。「行き来とは具体的にこうする」は本編に残す。

## 選定ルール
- 文字起こしはセグメント番号と時刻つきで与えられる。各シーンは必ず実在のセグメント番号で範囲指定する（秒数を創作しない）。
- 指定本数だけ、推奨順（先頭が最優先）に、範囲を重複させずに返す。
- **上位2本が実際に予告編へ採用される想定**（見どころ枠は概要テンプレを除いて約60秒）。
  3本目以降は人が差し替えを検討するための予備なので、上位2本とは異なる切り口・異なる時間帯から選ぶこと。
- **1シーンは20秒以上35秒以下**（30秒前後が目安）。**これは絶対の制約である。**
  各セグメントには時刻が付いているので、**end_seg の終了時刻 − start_seg の開始時刻を必ず計算し、35秒以内であることを確認してから出力する。**
  35秒に収まらないなら「話が成立する最小の塊」を選べていない。範囲を広げるのではなく、**より凝縮された別の箇所を選び直す**こと。
  同じ主張を長く繰り返している話者の場合は、**その主張が最も濃く出ている30秒だけ**を取る。前置きの経緯・データの読み上げ・繰り返しの言い換えは範囲に入れない。
  逆に、尺を埋めるためだけに無関係な前後を足してはならない。20秒に届かないなら、そのシーンは文脈が足りていないので選び直す。
- 冒頭に使える強い掴みを必ず1つ以上含める。
- **無音区間を絶対に含めないこと。** HRには「メモを取る時間を20秒ほど設けます」といった沈黙がある。
  文字起こし中の `----- 無音 N秒 -----` の行がそれで、**この線を跨ぐ範囲は指定しない**。
  尺を20秒に届かせるために沈黙を飲み込むくらいなら、別のシーンを選び直す。
- **引用文そのものは書かない。** 核心にあたる部分を quote_start_seg / quote_end_seg の**セグメント番号で指し示す**だけでよい。
  引用文はこちらが文字起こしから機械的に取り出す。文字起こしに誤変換があってもそのままにする（修正は telop 側で行う）。
  quote_start_seg / quote_end_seg は必ず start_seg 〜 end_seg の内側に収める。おおむね1〜3セグメント。
- **範囲＝意味の単位、quote＝その中の山場**である。
  核心が1文で済むからといって範囲を1文に縮めてはならない。ただし範囲の上限（35秒）が優先する。
- telop は画面に出す字幕文。欠けた前提文脈を補いつつ引きを残す短文（おおむね25字以内）。quoteの丸写しでなく、未視聴者に刺さる言い換え・要約でよい。
- **check には秒数の計算だけを書く。** 「seg147(00:11:23)〜seg152(00:11:51) = 28秒」の形で、**確定した範囲の**開始時刻・終了時刻・差を書く。
  20〜35秒に収まらない計算結果を書いたまま出力してはならない。収まるまで範囲を選び直してから書く。
- reason には「この範囲で話が成立する理由」と「どこで引きを作っているか」を1文で書く。人がレビューする際の判断材料になる。
  **reason に秒数の計算や検討過程を書かない**（それは check の役割）。

## 出力
JSON配列のみ。説明文は不要。キーはこの順で書く（check を先に計算してから範囲を確定させる）。
```json
[
  {
    "check": "seg145(00:11:10)〜seg149(00:11:38) = 28秒",
    "start_seg": 145,
    "end_seg": 149,
    "quote_start_seg": 147,
    "quote_end_seg": 148,
    "label": "シーンの一言ラベル（20字以内）",
    "telop": "画面に出す字幕（25字以内・引きを残す）",
    "reason": "前提が範囲内にあり単体で通じる。答えの手前で切って引きを残している。"
  }
]
```"""


def render_transcript(segments: list[dict]) -> str:
    lines = []
    prev_end = None
    for s in segments:
        # 無音は文字起こしに現れないため、明示的な区切り線として見せる。
        # これがないとAIは「メモを取る時間」を跨いだ範囲を平気で指定する。
        if prev_end is not None and s["start"] - prev_end >= GAP_SEC:
            lines.append(f"----- 無音 {s['start'] - prev_end:.0f}秒"
                         f"（この線を跨ぐ範囲は選ばない） -----")
        lines.append(f"[seg {s['idx']:04d} | {fmt_hhmmss(s['start'])}-"
                     f"{fmt_hhmmss(s['end'])}] {s['text']}")
        prev_end = s["end"]
    return "\n".join(lines)


# 直近の実行でかかったAPI費用（USD）。Webアプリ側が実行履歴に残すために読む。
# 戻り値を変えると呼び出し側の互換が崩れるので、ここに置いて main で拾う。
LAST_COST: dict[str, float] = {}
WHISPER_USD_PER_MIN = 0.006  # OpenAI whisper-1 の従量単価


def select_scenes(transcript: dict, *, num_candidates: int, model: str) -> list[dict]:
    import anthropic
    from transcribe_api import load_env_key

    api_key = load_env_key("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY が見つかりません。環境変数か "
            f"{PROJECT_ROOT / '.env'} に設定してください。"
        )
    client = anthropic.Anthropic(api_key=api_key)
    user_msg = (
        f"## 動画情報\n総尺: {fmt_hhmmss(transcript['duration'])} / "
        f"セグメント数: {len(transcript['segments'])}\n\n"
        f"## 採用本数\n推奨{num_candidates}本の見どころシーンを確定してください。\n\n"
        f"## 文字起こし\n{render_transcript(transcript['segments'])}"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=8192,
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    if resp.stop_reason == "max_tokens":
        print("  ⚠ 出力が max_tokens で切り詰められました")
    u = resp.usage
    # Sonnet 4.x の従量単価（USD / 1Mトークン）。概算表示用。
    cost = (getattr(u, "input_tokens", 0) * 3.0
            + getattr(u, "cache_creation_input_tokens", 0) * 3.75
            + getattr(u, "cache_read_input_tokens", 0) * 0.30
            + getattr(u, "output_tokens", 0) * 15.0) / 1_000_000
    print(f"    トークン: in {getattr(u, 'input_tokens', 0)} / "
          f"out {getattr(u, 'output_tokens', 0)} / "
          f"cache書込 {getattr(u, 'cache_creation_input_tokens', 0)} / "
          f"cache読込 {getattr(u, 'cache_read_input_tokens', 0)}")
    print(f"    概算コスト: ${cost:.4f}（約{cost * 155:.1f}円）")
    LAST_COST["select_usd"] = round(cost, 6)
    text = resp.content[0].text.strip()
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL) \
        or re.search(r"(\[.*\])", text, re.DOTALL)
    if not m:
        print("  Warning: JSONが見つかりません\n", text[:500])
        return []
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"  Warning: JSONパースエラー: {e}")
        return []


# ── 検証 & スナップ ───────────────────────────────────
def build_quote_index(segments: list[dict]) -> tuple[str, list[int]]:
    """全セグメントを正規化連結した文字列と、各文字の所属セグメント番号を返す。

    引用がどのセグメントに実在するかを文字単位で逆引きするための索引。
    """
    parts: list[str] = []
    owner: list[int] = []
    for s in segments:
        t = norm(s["text"])
        parts.append(t)
        owner.extend([s["idx"]] * len(t))
    return "".join(parts), owner


def locate_quote(quote: str, full: str, owner: list[int],
                 hint_seg: int) -> tuple[int, int] | None:
    """引用が実在するセグメント範囲を返す。無ければ None。

    同じ文言が複数箇所にある場合は、AIが申告した位置に最も近いものを選ぶ。
    """
    q = norm(quote)
    if not q:
        return None
    hits: list[tuple[int, int]] = []
    pos = full.find(q)
    while pos != -1:
        hits.append((owner[pos], owner[pos + len(q) - 1]))
        pos = full.find(q, pos + 1)
    if not hits:
        return None
    return min(hits, key=lambda h: abs(h[0] - hint_seg))


SPARSE_MIN_SEC = 6.0     # これ以上の長さで
SPARSE_MAX_CPS = 3.0     # 発話密度がこれ未満なら「ほぼ無音」とみなす
GAP_SEC = 2.0            # セグメント間にこれ以上の間があれば無音区間とみなす

# 冒頭がこれらで始まるシーンは、前の文を受けているので単体では通じない。
LEADING_CONNECTIVE_RE = re.compile(
    r"^(それ|その|これ|この|そう|そこで|そのため|なので|だから|ですから|"
    r"また|さらに|つまり|すなわち|逆に|一方|ただ|でも|しかし|で、|が、)")


def is_sparse(seg: dict) -> bool:
    """「メモを取る時間」等の無音・待機区間かを発話密度で判定する。

    HRには板書メモや問題文を読ませる長い沈黙があり、Whisperはその沈黙を
    直前の発話セグメントに含めてしまう。予告編に無音を入れないため除外する。
    """
    dur = seg["end"] - seg["start"]
    if dur < SPARSE_MIN_SEC:
        return False
    return len(norm(seg["text"])) / dur < SPARSE_MAX_CPS


def repair_quote(quote: str, full: str, owner: list[int],
                 hint_seg: int) -> tuple[str, tuple[int, int]] | None:
    """途中を飛ばした引用を、実在する最長の連続部分だけに切り詰める。

    AIは範囲の途中の文を省いて前後をつなぐことがある。その場合でも
    「実際に連続して話された部分」は必ず含まれているので、そこだけを残す。
    """
    sents = [s for s in re.split(r"(?<=[。！？])", quote) if s.strip()]
    if len(sents) < 2:
        return None
    best: tuple[str, tuple[int, int]] | None = None
    for i in range(len(sents)):
        for j in range(len(sents), i, -1):
            cand = "".join(sents[i:j])
            if len(norm(cand)) < 15:
                continue
            hit = locate_quote(cand, full, owner, hint_seg)
            if hit and (best is None or len(norm(cand)) > len(norm(best[0]))):
                best = (cand.strip(), hit)
                break
    return best


def validate_and_snap(raw: list[dict], transcript: dict,
                      duration: float) -> tuple[list[dict], list[str]]:
    segments = transcript["segments"]
    by_idx = {s["idx"]: s for s in segments}
    order = sorted(by_idx)
    pos = {k: i for i, k in enumerate(order)}
    full, owner = build_quote_index(segments)
    warnings: list[str] = []
    scenes: list[dict] = []
    for r in raw:
        si, ei = r.get("start_seg"), r.get("end_seg")
        if si not in by_idx or ei not in by_idx:
            warnings.append(f"存在しないセグメント番号: {si}-{ei}（除外）")
            continue
        if si > ei:
            si, ei = ei, si
            warnings.append(f"start>end を入替: seg {si}-{ei}")
        adjustments: list[str] = []

        # ① 引用文はAIに書かせず、指定されたセグメントから機械的に取り出す。
        #    AIは文字起こしの誤変換を善意で直してしまい逐語性が壊れるため、
        #    「どこが核心か」だけを指させて、文言はこちらが確定させる。
        qs, qe = r.get("quote_start_seg"), r.get("quote_end_seg")
        hit = None
        if isinstance(qs, int) and isinstance(qe, int):
            if qs > qe:
                qs, qe = qe, qs
            if qs in by_idx and qe in by_idx:
                if qs < si or qe > ei:
                    adjustments.append(
                        f"核心の指定 seg {qs}-{qe} がシーン範囲外だったため範囲を拡張")
                    si, ei = min(si, qs), max(ei, qe)
                hit = (qs, qe)
        if hit is None:
            # 旧形式（quote に文字列）へのフォールバック。
            legacy = r.get("quote", "")
            hit = locate_quote(legacy, full, owner, si)
            if hit is None and legacy:
                fixed = repair_quote(legacy, full, owner, si)
                if fixed:
                    adjustments.append("引用文が文字起こしと不一致のため実在部分に切り詰め")
                    hit = fixed[1]
            if hit is None:
                warnings.append(f"核心セグメントの指定が不正: seg {si}-{ei}")
                hit = (si, ei)
        quote = " ".join(by_idx[k]["text"] for k in range(hit[0], hit[1] + 1)
                         if k in by_idx)

        def dur_of(a: int, b: int) -> float:
            return min(by_idx[b]["end"], duration) - by_idx[a]["start"]

        def gap_before(k: int) -> float:
            """セグメント k の直前にある無音の長さ。"""
            i = pos[k]
            return 0.0 if i == 0 else by_idx[k]["start"] - by_idx[order[i - 1]]["end"]

        def dead(k: int, *, at_head: bool) -> bool:
            """端のセグメント k が無音・待機で、落としてよいか。"""
            if is_sparse(by_idx[k]):
                return True
            # 端を落とすと、その外側に接する無音も一緒に消える。
            return (gap_before(order[pos[k] + 1]) >= GAP_SEC if at_head
                    else gap_before(k) >= GAP_SEC)

        # ② 無音・待機区間（メモを取る時間など）を範囲の端から削る。
        #    引用が実在する範囲は必ず残す。
        keep_lo, keep_hi = (hit if hit else (si, ei))
        before = (si, ei)
        while ei > keep_hi and dead(ei, at_head=False):
            ei = order[pos[ei] - 1]
        while si < keep_lo and dead(si, at_head=True):
            si = order[pos[si] + 1]
        if (si, ei) != before:
            adjustments.append(
                f"無音・メモ時間を除外して seg {before[0]}-{before[1]} → {si}-{ei}")

        # ③ 短すぎるシーンは前後のセグメントを足して下限まで伸ばす。
        #    ただし無音区間は跨がない（跨ぐと予告編に沈黙が入る）。
        if dur_of(si, ei) < MIN_SCENE_SEC:
            before = (si, ei)
            lo, hi = pos[si], pos[ei]
            while dur_of(order[lo], order[hi]) < MIN_SCENE_SEC:
                grew = False
                if (hi + 1 < len(order) and not is_sparse(by_idx[order[hi + 1]])
                        and gap_before(order[hi + 1]) < GAP_SEC
                        and dur_of(order[lo], order[hi + 1]) <= MAX_SCENE_SEC):
                    hi += 1
                    grew = True
                elif (lo - 1 >= 0 and not is_sparse(by_idx[order[lo - 1]])
                      and gap_before(order[lo]) < GAP_SEC
                      and dur_of(order[lo - 1], order[hi]) <= MAX_SCENE_SEC):
                    lo -= 1
                    grew = True
                if not grew:
                    break
            si, ei = order[lo], order[hi]
            if (si, ei) != before:
                # 機械的な水増しは「意味の単位として選べていない」兆候。
                # 黙って通さず、レビュー担当者に文脈の確認を促す。
                adjustments.append(
                    f"尺が {dur_of(*before):.0f}s と短く機械的に拡張 "
                    f"（seg {before[0]}-{before[1]} → {si}-{ei}）。"
                    f"前後が意味のある文脈になっているか要確認")

        # ④ 長すぎるシーンは核心に向かって詰める。
        #    話し方が冗長なHRでは、AIが上限を無視して1分超を返してくる。
        #    核心から遠い側の端から落とし、山場を中心に据える。
        if dur_of(si, ei) > MAX_SCENE_SEC:
            before = (si, ei)
            while dur_of(si, ei) > MAX_SCENE_SEC:
                head_room = by_idx[keep_lo]["start"] - by_idx[si]["start"]
                tail_room = by_idx[ei]["end"] - by_idx[keep_hi]["end"]
                if tail_room >= head_room and ei > keep_hi:
                    ei = order[pos[ei] - 1]
                elif si < keep_lo:
                    si = order[pos[si] + 1]
                else:
                    break  # 核心そのものが上限を超えている
            if (si, ei) != before:
                adjustments.append(
                    f"尺が {dur_of(*before):.0f}s と長く核心に向けて短縮 "
                    f"（seg {before[0]}-{before[1]} → {si}-{ei}）。"
                    f"前提が欠けていないか要確認")

        # ⑤ 範囲内に無音区間が残っていれば人に知らせる（自動では切れない）。
        inner_sparse = [k for k in range(si, ei + 1)
                        if k in by_idx and (is_sparse(by_idx[k])
                                            or (k > si and gap_before(k) >= GAP_SEC))]
        if inner_sparse:
            warnings.append(
                f"範囲内に無音・メモ時間あり: seg {inner_sparse}（{fmt_hhmmss(by_idx[si]['start'])}〜）")

        # ⑥ 冒頭が前の文を受ける語なら、単体では話が通じない可能性が高い。
        if LEADING_CONNECTIVE_RE.match(norm(by_idx[si]["text"])):
            warnings.append(
                f"冒頭が前を受ける語で始まる: 「{by_idx[si]['text'][:20]}…」"
                f"（{fmt_hhmmss(by_idx[si]['start'])}・前提が欠けている可能性）")

        start = by_idx[si]["start"]
        end = min(by_idx[ei]["end"], duration)
        dur = end - start
        if dur > MAX_SCENE_SEC:
            warnings.append(
                f"尺が長い: seg {si}-{ei} は {dur:.0f}s "
                f"（上限 {MAX_SCENE_SEC}s・短縮不可）")
        elif dur < MIN_SCENE_SEC:
            warnings.append(
                f"尺が短い: seg {si}-{ei} は {dur:.0f}s "
                f"（下限 {MIN_SCENE_SEC}s・拡張不可）")
        warnings.extend(adjustments)
        scenes.append({
            "start_seg": si, "end_seg": ei,
            "start_sec": start, "end_sec": end,
            "duration_sec": round(end - start, 2),
            "label": r.get("label", ""), "quote": quote,
            "telop": r.get("telop", ""),
            "reason": r.get("reason", ""),
            # 範囲で実際に話されている全文。レビュー担当者が動画を見ずに
            # 「この尺だけで話が通じるか」を判断するために必須。
            "scene_text": " ".join(by_idx[k]["text"] for k in range(si, ei + 1)
                                   if k in by_idx),
            "snapped": True,
            "adjustments": adjustments,
        })
    # 重複解消（推奨順で先に来たものを優先して残す）
    kept: list[dict] = []
    for s in scenes:
        if any(not (s["end_sec"] <= k["start_sec"] or s["start_sec"] >= k["end_sec"])
               for k in kept):
            warnings.append(f"重複により除外: {s['label']} ({fmt_hhmmss(s['start_sec'])})")
            continue
        kept.append(s)
    for rank, s in enumerate(kept, 1):
        s["rank"] = rank
    return kept, warnings


# ── ground-truth 突合 ─────────────────────────────────
def locate_digest(digest_tr: dict, source_tr: dict) -> list[tuple[float, float]]:
    """人間ダイジェスト各セグメントを元HR内のどこかに照合し区間を復元する。

    ダイジェストは元HRから切り出した映像なので、同じ音声を同じWhisperに
    かければ文字列はほぼ一致する。そこで最長共通部分文字列で位置を当てる。
    セグメント単位で比較すると、区切り位置が両者でずれた時に一致率が落ちて
    取りこぼすため、元HR全体を1本の文字列として扱う。
    """
    src = source_tr["segments"]
    full, owner = build_quote_index(src)
    intervals: list[tuple[float, float]] = []
    by_idx = {s["idx"]: s for s in src}
    for d in digest_tr["segments"]:
        dn = norm(d["text"])
        if len(dn) < 6:
            continue
        m = SequenceMatcher(None, dn, full, autojunk=False) \
            .find_longest_match(0, len(dn), 0, len(full))
        # 半分以上が連続一致していれば同じ発話とみなす。
        if m.size < max(6, len(dn) * 0.5):
            continue
        a, b = owner[m.b], owner[m.b + m.size - 1]
        intervals.append((by_idx[a]["start"], by_idx[b]["end"]))
    # マージ
    intervals.sort()
    merged: list[list[float]] = []
    for s, e in intervals:
        if merged and s <= merged[-1][1] + 5:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(a, b) for a, b in merged]


def overlap_seconds(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    total = 0.0
    for s1, e1 in a:
        for s2, e2 in b:
            total += max(0.0, min(e1, e2) - max(s1, s2))
    return total


def span_seconds(iv: list[tuple[float, float]]) -> float:
    return sum(e - s for s, e in iv)


# ── 出力 ──────────────────────────────────────────────
def write_outputs(video: Path, transcript: dict, scenes: list[dict],
                  warnings: list[str], duration: float, params: dict,
                  gt: dict | None, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = video.stem
    plan = {
        "schema_version": 1, "created_at": now_iso(),
        "source_video": str(video.resolve()),
        "video_duration_sec": duration, "params": params,
        "total_runtime_sec": round(sum(s["duration_sec"] for s in scenes), 2),
        "scenes": scenes, "warnings": warnings,
    }
    if gt:
        plan["ground_truth"] = gt
    json_path = out_dir / f"{stem}_digest_plan.json"
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    md = [f"# ダイジェスト見どころ: {stem}", "",
          f"- 元動画: `{video.name}`（{fmt_hhmmss(duration)}）",
          f"- 候補: {len(scenes)}本（上位2本が採用想定 / "
          f"上位2本の合計尺: {fmt_hhmmss(sum(s['duration_sec'] for s in scenes[:2]))}）",
          f"- 全候補の合計尺: {fmt_hhmmss(plan['total_runtime_sec'])}"
          f" / 1シーン想定 {MIN_SCENE_SEC}〜{MAX_SCENE_SEC}秒",
          f"- Whisper: {params['whisper_model']} / "
          f"選定: {params.get('select_model') or params.get('selection_source', '不明')}",
          "",
          "> ⚠ **本文の引用はすべて自動文字起こしです。固有名詞を中心に誤変換があります**"
          "（例:「京大」が「兄弟」になる）。",
          "> 実際に何と言っているかは必ず映像で確認してください。時刻は音声に合わせてあります。",
          "", "## 採用シーン（推奨順）", ""]
    for s in scenes:
        md.append(f"### {s['rank']}. {s['label']}"
                  f"（{fmt_hhmmss(s['start_sec'])}–{fmt_hhmmss(s['end_sec'])} / "
                  f"{int(s['duration_sec'])}s）")
        md.append(f"- **シーン**: {fmt_hhmmss(s['start_sec'])}–{fmt_hhmmss(s['end_sec'])}")
        md.append(f"- **テロップ**: {s['telop']}")
        md.append(f"- **核心の一言**（自動文字起こし・要確認）: {s['quote']}")
        if s.get("reason"):
            md.append(f"- **選定理由**: {s['reason']}")
        if s.get("adjustments"):
            md.append(f"- ⚠ **機械補正あり**: {' / '.join(s['adjustments'])}")
        # 動画を見ずに「この尺だけで話が通じるか」を判断するための全文。
        md.append(f"- **このシーンで話される内容（全文・自動文字起こし）**:\n"
                  f"  > {s.get('scene_text', '')}\n")
    if gt:
        md += ["## 人間ダイジェストとの突合", "",
               f"- 人間ダイジェスト尺: {fmt_hhmmss(gt['human_span_sec'])}",
               f"- 元HR内で復元できた区間: {len(gt['human_intervals'])}箇所",
               f"- システム候補と重なった秒数: {gt['overlap_sec']:.1f}s",
               f"- カバレッジ(recall): {gt['recall']:.0%}（人間採用箇所のうちシステムも拾えた割合）", ""]
        md.append("人間が採用した区間（元HR時刻）:")
        for s, e in gt["human_intervals"]:
            md.append(f"- {fmt_hhmmss(s)}-{fmt_hhmmss(e)}")
    if warnings:
        md += ["", "## 警告"] + [f"- {w}" for w in warnings]
    md_path = out_dir / f"{stem}_digest_plan.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    write_csv(stem, scenes, out_dir)
    return json_path, md_path


# ツールが記入する列と、レビュー担当者が記入する列。
# 再実行でツールが上書きするのは前半だけ、という境界を表側でも分かるようにする。
CSV_HEADER = ["HRコード", "rank", "区分", "IN", "OUT", "尺(秒)", "ラベル",
              "抜粋（自動文字起こし・誤変換の可能性あり）", "テロップ案", "警告",
              "採用", "テロップ確定", "IN修正", "OUT修正", "メモ"]
HR_CODE_RE = re.compile(r"\d{2}-[A-Z]{2}-[a-z]{2}-\d{2}")


def write_csv(stem: str, scenes: list[dict], out_dir: Path) -> Path:
    """編集者が切り抜き時刻を参照するための一覧表（1シーン＝1行）。"""
    code = m.group(0) if (m := HR_CODE_RE.search(stem)) else stem
    path = out_dir / f"{stem}_digest_scenes.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for s in scenes:
            notes = list(s.get("adjustments") or [])
            if s["duration_sec"] < MIN_SCENE_SEC:
                notes.append(f"尺が下限{MIN_SCENE_SEC}秒未満")
            elif s["duration_sec"] > MAX_SCENE_SEC:
                notes.append(f"尺が上限{MAX_SCENE_SEC}秒超")
            w.writerow([
                code, s["rank"], "推奨" if s["rank"] <= 2 else "予備",
                fmt_hhmmss(s["start_sec"]), fmt_hhmmss(s["end_sec"]),
                int(round(s["duration_sec"])), s["label"],
                s["quote"], s["telop"], " / ".join(notes),
                "", "", "", "", "",
            ])
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="4HRダイジェスト見どころ選定 PoC")
    ap.add_argument("--video", required=True)
    ap.add_argument("--human-digest", help="比較用の人間制作ダイジェストmp4")
    ap.add_argument("--num-candidates", type=int, default=3,
                    help="採用する見どころ本数（推奨3）")
    ap.add_argument("--whisper-model", default=MODEL_API,
                    help="既定は api-whisper（OpenAI）。faster-whisperのモデル名も指定可")
    ap.add_argument("--language", default="ja")
    ap.add_argument("--model", default=SELECT_MODEL)
    ap.add_argument("--scenes-file", type=Path,
                    help="Claude選定を迂回し、事前選定シーンJSONを読み込む（PoC/オフライン用）")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--transcribe-only", action="store_true",
                    help="文字起こしまでで停止（Claude選定をしない）")
    args = ap.parse_args()

    video = Path(args.video)
    if not video.exists():
        print(f"Error: 動画が見つかりません: {video}", file=sys.stderr)
        return 1

    print(f"📹 {video.name}")
    duration = probe_duration(video)
    transcript = load_or_transcribe(video, model_size=args.whisper_model,
                                    language=args.language, no_cache=args.no_cache)
    print(f"    → セグメント {len(transcript['segments'])}件 / "
          f"文字起こし {transcript.get('transcribe_seconds', 0):.0f}秒")

    if args.transcribe_only:
        print("\n--- 文字起こし冒頭プレビュー ---")
        for s in transcript["segments"][:25]:
            print(f"[{fmt_hhmmss(s['start'])}] {s['text']}")
        return 0

    if args.scenes_file:
        print(f"    事前選定シーン読込: {args.scenes_file.name}")
        raw = json.loads(args.scenes_file.read_text(encoding="utf-8"))
    else:
        print("    Claude 見どころ選定中…")
        raw = select_scenes(transcript, num_candidates=args.num_candidates, model=args.model)
    scenes, warnings = validate_and_snap(raw, transcript, duration)
    print(f"    → 候補 {len(scenes)}件（警告 {len(warnings)}件）")

    gt = None
    if args.human_digest:
        hd = Path(args.human_digest)
        if hd.exists():
            print(f"🎯 人間ダイジェスト突合: {hd.name}")
            digest_tr = load_or_transcribe(hd, model_size=args.whisper_model,
                                           language=args.language, no_cache=args.no_cache)
            human_iv = locate_digest(digest_tr, transcript)
            sys_iv = [(s["start_sec"], s["end_sec"]) for s in scenes]
            ov = overlap_seconds(human_iv, sys_iv)
            hspan = span_seconds(human_iv)
            gt = {"human_intervals": human_iv,
                  "human_span_sec": round(hspan, 2),
                  "overlap_sec": round(ov, 2),
                  "recall": round(ov / hspan, 4) if hspan else 0.0}
            print(f"    → recall {gt['recall']:.0%} / overlap {ov:.1f}s / "
                  f"人間区間 {len(human_iv)}箇所")

    params = {"whisper_model": args.whisper_model,
              "num_candidates": args.num_candidates}
    if args.scenes_file:
        # 事前選定ファイルを読んだ場合、Claudeは一切呼んでいない。
        # これを select_model として記録すると「AIが選んだ」と誤読される。
        params["selection_source"] = f"scenes-file: {args.scenes_file.name}"
        params["select_model"] = None
    else:
        params["selection_source"] = "claude-api"
        params["select_model"] = args.model
    # 概算コスト。Webアプリが実行履歴に出す。キャッシュ利用時は文字起こし費用0。
    params["select_cost_usd"] = LAST_COST.get("select_usd")
    params["transcribe_cost_usd"] = (
        0.0 if transcript.get("from_cache")
        else round(duration / 60 * WHISPER_USD_PER_MIN, 6))
    json_path, md_path = write_outputs(video, transcript, scenes, warnings,
                                       duration, params, gt, args.out_dir)
    print(f"\n✅ 出力: {md_path}\n          {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
