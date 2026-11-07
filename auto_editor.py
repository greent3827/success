#!/usr/bin/env python3
"""
auto_editor.py — 영상 1차 자동 편집기

하는 일
  1) 음성 인식(faster-whisper)으로 단어 단위 대본 만들기
  2) 컷 편집: 무음 구간 제거 + 필러워드("어", "음" …) 제거
     - 문장 사이엔 짧은 숨 고르기를 남겨서 말이 뚝뚝 끊기지 않게 함
     - Claude API 키가 있으면 대본 맥락을 보고 "지워도 이해에 지장 없는 필러"만 지움
  3) 설명 화면: 이해를 돕는 순간에 '플랫 벡터 + 핸드드로잉 카툰' 일러스트를 넣음
     - Claude가 대본을 읽고 위치·장면을 정하고, 이미지 AI(OpenAI 또는 Gemini)가 그림을 그림
     - 그림 안에는 글자를 넣지 않고, 필요할 때만 8자 이내 한글 키워드를 아래에 얹음
  4) 개인정보 가리기
     - 화면 속 글자(전화번호·이메일·주민번호·차량번호·계좌·주소) → OCR로 찾아 모자이크
     - 말로 한 개인정보(전화번호·주소·실명 등) → '삐' 처리
     - --blur-faces 를 주면 등장하는 얼굴도 모자이크
  5) 결과: <영상이름>_edited/ 폴더에 편집본 mp4 + 편집 보고서(report.md) + 계획 파일(plan.json)

다시 실행하면 대본·계획·그림을 재사용합니다. plan.json 을 고치거나
illustrations/ 폴더의 그림을 바꿔 넣고 다시 실행하면 그대로 반영됩니다.

사용 예
  python auto_editor.py 내영상.mp4
  python auto_editor.py 내영상.mp4 --blur-faces
  python auto_editor.py 내영상.mp4 --illustration-mode overlay
  python auto_editor.py --watch 업로드폴더          # 폴더에 영상이 들어오면 자동 처리
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SR = 48000  # 작업용 오디오 샘플레이트


# ─────────────────────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(msg, flush=True)


def need(binary: str) -> None:
    if shutil.which(binary) is None:
        sys.exit(f"[오류] '{binary}' 를 찾을 수 없습니다. README 의 설치 방법을 확인하세요.")


def probe(video: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height,r_frame_rate,avg_frame_rate:format=duration",
         "-of", "json", str(video)], capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    v = next(s for s in info["streams"] if s.get("codec_type") == "video")
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])

    def rate(r):
        try:
            n, d = r.split("/")
            return float(n) / float(d) if float(d) else 0
        except Exception:
            return 0
    fps = rate(v.get("avg_frame_rate", "0/0")) or rate(v.get("r_frame_rate", "0/0"))
    fps = fps if 1 < fps < 121 else 30.0
    return {"w": int(v["width"]), "h": int(v["height"]), "fps": fps,
            "duration": float(info["format"]["duration"]), "audio": has_audio}


def fmt_ts(t: float) -> str:
    m, s = divmod(max(t, 0), 60)
    h, m = divmod(int(m), 60)
    return f"{h:d}:{m:02d}:{s:05.2f}" if h else f"{m:02d}:{s:05.2f}"


def http_json(url: str, body: dict, headers: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}")


def find_font(bold: bool = True) -> str | None:
    cands = {
        "Windows": [r"C:\Windows\Fonts\malgunbd.ttf", r"C:\Windows\Fonts\malgun.ttf"],
        "Darwin": ["/System/Library/Fonts/AppleSDGothicNeo.ttc"],
    }.get(platform.system(), []) + [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    ]
    for c in cands:
        if Path(c).exists():
            return c
    return None


# ─────────────────────────────────────────────────────────────
# 1) 음성 인식
# ─────────────────────────────────────────────────────────────
# Whisper 는 "어, 음" 같은 말을 대본에서 빼버리는 버릇이 있어서,
# 필러가 섞인 예시 문장을 프롬프트로 주면 들리는 그대로 받아 적는 경향이 강해집니다.
FILLER_PROMPT = "음, 어, 그러니까 음... 어 네, 아 그게요, 어 그래서 음 이렇게 됐어요."


def transcribe(video: Path, model_size: str, language: str) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("[오류] faster-whisper 가 없습니다:  pip install faster-whisper")
    log(f"▶ 음성 인식 중… (모델: {model_size}, 영상 길이에 따라 수 분 걸립니다)")
    model = WhisperModel(model_size, device="auto", compute_type="auto")
    segments, _ = model.transcribe(
        str(video), language=language, word_timestamps=True,
        initial_prompt=FILLER_PROMPT, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300}, beam_size=5)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word})
        log(f"   {fmt_ts(seg.end)}  {seg.text.strip()[:60]}")
    return {"language": language, "words": words}


# ─────────────────────────────────────────────────────────────
# 2) 문장 묶기 + 필러 후보 찾기
# ─────────────────────────────────────────────────────────────
@dataclass
class Sentence:
    idx: int
    start: float
    end: float
    text: str
    wids: list = field(default_factory=list)   # words 리스트의 인덱스


END_PUNCT = re.compile(r"[.?!…。？！]$")
END_KO = re.compile(r"(다|요|죠|까|네|니다|세요|군요|거든요|잖아요|래요)[.?!…]*$")


def build_sentences(words: list, pause_split: float = 0.9, max_len: float = 15.0) -> list[Sentence]:
    sents, cur = [], []

    def flush():
        if cur:
            text = "".join(words[k]["word"] for k in cur).strip()
            if text:
                sents.append(Sentence(len(sents), words[cur[0]]["start"],
                                      words[cur[-1]]["end"], text, list(cur)))
        cur.clear()

    for i, w in enumerate(words):
        cur.append(i)
        tok = w["word"].strip()
        gap = words[i + 1]["start"] - w["end"] if i + 1 < len(words) else 99
        span = w["end"] - words[cur[0]]["start"]
        if (END_PUNCT.search(tok) or (gap > 0.45 and END_KO.search(tok))
                or gap > pause_split or span > max_len):
            flush()
    flush()
    return sents


def norm_tok(tok: str) -> str:
    return re.sub(r"[\s,.…~!?\-]", "", tok)


# 확실한 필러: 무조건 지움
FILLER_SURE = re.compile(r"^(어+|음+|으+음*|엄+|흠+|어+음+|으+|어+어+)$")
# 애매한 필러: 앞뒤로 쉼이 있을 때만(=혼자 떨어져 있는 감탄사일 때만) 지움
FILLER_MAYBE = re.compile(r"^(아+|에+|그+|저+|막|뭐랄까|약간)$")


def filler_candidates(words: list) -> dict[int, str]:
    """{단어인덱스: 'sure' | 'maybe'}"""
    out = {}
    for i, w in enumerate(words):
        t = norm_tok(w["word"])
        if not t:
            continue
        if FILLER_SURE.match(t):
            out[i] = "sure"
        elif FILLER_MAYBE.match(t):
            gb = w["start"] - words[i - 1]["end"] if i > 0 else 9
            ga = words[i + 1]["start"] - w["end"] if i + 1 < len(words) else 9
            if min(gb, ga) >= 0.2 or (gb >= 0.5 and ga >= 0.12):
                out[i] = "maybe"
    return out


# ─────────────────────────────────────────────────────────────
# 3) 말로 한 개인정보(대본 정규식) — Claude 가 없을 때의 기본 방어선
# ─────────────────────────────────────────────────────────────
PII_TEXT_PATTERNS = {
    "전화번호": r"0\d{1,2}[\s\-.)]*\d{3,4}[\s\-.]*\d{4}",
    "주민번호": r"\d{6}\s*-\s*[1-4]\d{6}",
    "카드번호": r"\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}",
    "계좌번호": r"\d{2,6}-\d{2,6}-\d{2,8}",
    "이메일": r"[\w.+\-]+@[\w\-]+\.[\w.]+",
    "차량번호": r"\d{2,3}\s?[가-힣]\s?\d{4}",
}


def locate_text_in_words(words: list, wids: list, needle: str) -> list[int]:
    """문장(wids) 안에서 needle(공백 무시)과 겹치는 단어 인덱스들."""
    hay, owner = "", []
    for k in wids:
        t = re.sub(r"\s", "", words[k]["word"])
        hay += t
        owner += [k] * len(t)
    n = re.sub(r"\s", "", needle)
    if not n:
        return []
    p = hay.find(n)
    if p < 0:
        return []
    return sorted(set(owner[p:p + len(n)]))


def pii_regex_spoken(words: list, sents: list[Sentence]) -> list[dict]:
    found = []
    for s in sents:
        flat = "".join(re.sub(r"\s", "", words[k]["word"]) for k in s.wids)
        for kind, pat in PII_TEXT_PATTERNS.items():
            for m in re.finditer(pat, flat):
                ids = locate_text_in_words(words, s.wids, m.group(0))
                if ids:
                    found.append({"type": kind, "text": m.group(0), "sentence": s.idx, "wids": ids})
    return found


# ─────────────────────────────────────────────────────────────
# 4) Claude: 필러 확정 + 말 개인정보 + 설명 화면 계획
# ─────────────────────────────────────────────────────────────
CLAUDE_PROMPT = """당신은 유튜브 영상 편집자입니다. 아래는 영상 대본이고, 각 줄은 [문장번호] (시작시각) 문장 입니다.
⟨f번호:말⟩ 로 표시된 부분은 필러워드(어, 음 같은 군말) 후보입니다.

세 가지 일을 해 주세요.

1) 필러 확정: 후보 중 지워도 문장 뜻과 이해에 전혀 지장이 없는 것만 고르세요.
   - "어", "음"처럼 의미 없는 망설임 소리는 지웁니다.
   - "아"가 깨달음·감탄으로 뜻을 전하거나, "그", "저"가 실제로 대상을 가리키는 말이면 남깁니다.

2) 말로 드러난 개인정보: 일반인의 실명, 전화번호, 상세 주소(동·호수·번지), 계좌·카드·주민번호,
   차량번호, 개인 이메일을 말한 부분을 찾으세요. 유명인·브랜드·지역명 수준(예: "서울")·채널명은 제외합니다.
   "text" 에는 대본에 적힌 그대로의 글자를 넣으세요(공백은 달라도 됨).

3) 설명 화면: 시청자 이해를 확실히 도와주는 순간을 최대 {max_ill}개 고르세요.
   - 추상적 개념, 과정·순서, 비교, 숫자·비율, 원리 설명처럼 '그림 하나면 바로 이해되는' 곳만 고릅니다.
   - 이미 화면만 봐도 알 수 있는 내용, 잡담, 인사는 고르지 마세요. 해당 없으면 개수를 줄이세요.
   - 서로 최소 8초 이상 떨어지게 고르세요.
   - "prompt" 는 이미지 AI에게 줄 영어 장면 묘사입니다. 개념 하나를 구체적인 사물·인물·은유로 표현하고,
     글자가 들어갈 요소(간판, 칠판 글씨, 라벨, 숫자)는 넣지 마세요. 스타일 문구는 쓰지 마세요(자동으로 붙음).
   - "label" 은 그림 아래 띄울 한국어 키워드로 8자 이내, 그림만으로 충분하면 빈 문자열("")로 두세요.
   - "duration" 은 2.5~4.5초.

오직 JSON 객체만 출력하세요(설명 금지):
{{"fillers_cut": [필러번호, ...],
  "pii": [{{"sentence": 문장번호, "text": "대본의 해당 부분", "type": "종류"}}],
  "illustrations": [{{"sentence": 문장번호, "duration": 3.5, "label": "키워드 또는 빈칸", "prompt": "English scene", "why": "왜 필요한지 한 문장"}}]}}

대본:
{transcript}"""


def annotated_transcript(words: list, sents: list[Sentence], cands: dict) -> str:
    lines = []
    for s in sents:
        parts = []
        for k in s.wids:
            tok = words[k]["word"]
            if k in cands:
                lead = " " if tok.startswith(" ") else ""
                parts.append(f"{lead}⟨f{k}:{tok.strip()}⟩")
            else:
                parts.append(tok)
        lines.append(f"[{s.idx}] ({fmt_ts(s.start)}) {''.join(parts).strip()}")
    return "\n".join(lines)


def ask_claude(words, sents, cands, max_ill, model, api_key) -> dict:
    prompt = CLAUDE_PROMPT.format(max_ill=max_ill,
                                  transcript=annotated_transcript(words, sents, cands))
    data = http_json("https://api.anthropic.com/v1/messages",
                     {"model": model, "max_tokens": 8000,
                      "messages": [{"role": "user", "content": prompt}]},
                     {"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    text = "".join(b.get("text", "") for b in data.get("content", []))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("응답에서 JSON 을 찾지 못했습니다: " + text[:200])
    return json.loads(m.group(0))


def make_plan(words, sents, cands, args, max_ill) -> dict:
    """Claude(있으면) 또는 규칙 기반으로 편집 계획을 만든다."""
    plan = {"fillers_cut": [], "pii_spoken": [], "illustrations": [], "planner": "local"}
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    result = None
    if key and not args.no_claude:
        log(f"▶ Claude({args.model})가 대본을 읽고 필러·개인정보·설명 화면을 정하는 중…")
        try:
            result = ask_claude(words, sents, cands, max_ill, args.model, key)
            plan["planner"] = "claude"
        except Exception as e:
            log(f"   Claude 호출 실패({e}) → 규칙 기반으로 진행")
    elif not key:
        log("   [안내] ANTHROPIC_API_KEY 가 없어 규칙 기반으로 컷 편집만 하고, 설명 화면은 건너뜁니다.")

    # 필러
    if result:
        ok = {int(x) for x in result.get("fillers_cut", []) if int(x) in cands}
        # 확실한 필러("어", "음")는 Claude 가 빠뜨려도 지움
        ok |= {k for k, v in cands.items() if v == "sure"}
        plan["fillers_cut"] = sorted(ok)
    else:
        plan["fillers_cut"] = sorted(cands)

    # 말 개인정보 (정규식 + Claude)
    spoken = pii_regex_spoken(words, sents)
    if result:
        for p in result.get("pii", []):
            try:
                s = sents[int(p["sentence"])]
            except (KeyError, ValueError, IndexError):
                continue
            ids = locate_text_in_words(words, s.wids, str(p.get("text", "")))
            if ids:
                spoken.append({"type": p.get("type", "개인정보"), "text": p["text"],
                               "sentence": s.idx, "wids": ids})
    seen, uniq = set(), []
    for p in spoken:
        key_ = tuple(p["wids"])
        if key_ not in seen:
            seen.add(key_)
            uniq.append(p)
    plan["pii_spoken"] = uniq

    # 설명 화면
    if result:
        ills = []
        for n, it in enumerate(result.get("illustrations", [])):
            try:
                si = int(it["sentence"])
                assert 0 <= si < len(sents) and it.get("prompt")
            except Exception:
                continue
            ills.append({"sentence": si,
                         "duration": float(min(max(float(it.get("duration", 3.5)), 2.0), 5.0)),
                         "label": str(it.get("label", ""))[:8],
                         "prompt": str(it["prompt"]),
                         "why": str(it.get("why", "")),
                         "image": f"ill_{n + 1:02d}.png"})
        plan["illustrations"] = ills[:max_ill]
    return plan


# ─────────────────────────────────────────────────────────────
# 5) 컷 구간 계산 (무음 + 필러)
# ─────────────────────────────────────────────────────────────
def load_audio(video: Path, has_audio: bool, duration: float) -> np.ndarray:
    """(N, 2) float32, 48kHz."""
    if not has_audio:
        return np.zeros((int(duration * SR), 2), np.float32)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "2",
                          "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2).copy()


def silent_runs(audio: np.ndarray, min_len: float, thr_db: float | None) -> tuple[list, float]:
    """소리 크기가 문턱보다 낮은 구간 [(start, end)] 과 사용한 문턱(dB)."""
    hop = int(SR * 0.02)
    mono = audio.mean(axis=1)
    n = len(mono) // hop
    if n == 0:
        return [], -60
    rms = np.sqrt((mono[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-9)
    if thr_db is None:
        floor, speech = np.percentile(db, 10), np.percentile(db, 90)
        thr_db = float(max(floor + (speech - floor) * 0.3, -55))
    quiet = db < thr_db
    runs, start = [], None
    for i, q in enumerate(np.append(quiet, False)):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * 0.02 >= min_len:
                runs.append((start * 0.02, i * 0.02))
            start = None
    return runs, thr_db


def subtract(intervals: list, holes: list) -> list:
    out = []
    for s, e in intervals:
        pieces = [(s, e)]
        for hs, he in holes:
            nxt = []
            for a, b in pieces:
                if he <= a or hs >= b:
                    nxt.append((a, b))
                else:
                    if hs > a:
                        nxt.append((a, hs))
                    if he < b:
                        nxt.append((he, b))
            pieces = nxt
        out += pieces
    return out


def merge(intervals: list, gap: float = 0.0) -> list:
    out = []
    for s, e in sorted(intervals):
        if out and s - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def compute_keep(words, sents, fillers_cut, audio, duration, args) -> tuple[list, dict]:
    sent_end_ids = {s.wids[-1] for s in sents}
    sent_start_ids = {s.wids[0] for s in sents}
    cut = set(fillers_cut)
    spans = []
    for i, w in enumerate(words):
        if i in cut:
            continue
        before = args.pad + (0.06 if i in sent_start_ids else 0)
        after = args.pad + (args.sentence_breath if i in sent_end_ids else 0.04)
        spans.append((max(0.0, w["start"] - before), min(duration, w["end"] + after)))
    # 말 사이 짧은 쉼은 그대로 둠(자연스러움), 긴 쉼만 잘라냄
    keep = merge(spans, gap=args.max_pause)
    # Whisper 타임스탬프가 무음까지 늘어난 경우 대비: 긴 무음은 안쪽에서도 줄임
    runs, thr = silent_runs(audio, args.max_pause + 0.25, args.silence_db)
    holes = [(a + 0.12, b - 0.12) for a, b in runs]
    keep = subtract(keep, holes)
    # 필러가 차지하던 자리는 정확히 비움
    keep = subtract(keep, [(words[k]["start"], words[k]["end"]) for k in cut])
    keep = [(s, e) for s, e in merge(keep, 0.04) if e - s >= 0.12]
    stats = {"silence_threshold_db": round(thr, 1)}
    return keep, stats


def snap_to_frames(keep: list, fps: float, n_frames: int) -> list[tuple[int, int]]:
    out = []
    for s, e in keep:
        a, b = int(round(s * fps)), min(int(round(e * fps)), n_frames)
        if b - a >= 2:
            if out and a <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], b))
            else:
                out.append((a, b))
    return out


class TimeMap:
    """원본 시각 ↔ 편집본 시각 변환."""

    def __init__(self, frame_iv: list, fps: float):
        self.iv = frame_iv
        self.fps = fps
        self.cum = np.cumsum([0] + [b - a for a, b in frame_iv])

    def to_out(self, t_src: float) -> float:
        f = t_src * self.fps
        for k, (a, b) in enumerate(self.iv):
            if f < a:
                return self.cum[k] / self.fps          # 잘린 곳 → 다음 살아남은 지점
            if f < b:
                return (self.cum[k] + f - a) / self.fps
        return self.cum[-1] / self.fps

    @property
    def out_duration(self) -> float:
        return self.cum[-1] / self.fps


# ─────────────────────────────────────────────────────────────
# 6) 일러스트 생성 (OpenAI / Gemini)
# ─────────────────────────────────────────────────────────────
STYLE = ("Flat vector illustration blended with a hand-drawn cartoon look: slightly wobbly black ink "
         "outlines like a marker sketch, flat solid color fills, soft limited palette (warm cream "
         "background, muted teal, coral, mustard yellow, charcoal), simple friendly rounded characters "
         "with dot eyes, subtle paper texture, lots of clean empty space, one clear focal concept, "
         "no gradients, no photorealism, no 3D. "
         "IMPORTANT: absolutely no text, letters, words, numbers, captions, labels, logos or watermarks anywhere.")


def gen_openai(prompt: str, portrait: bool, model: str, key: str) -> bytes:
    size = "1024x1536" if portrait else "1536x1024"
    data = http_json("https://api.openai.com/v1/images/generations",
                     {"model": model, "prompt": prompt, "size": size, "n": 1},
                     {"Authorization": f"Bearer {key}"}, timeout=300)
    item = data["data"][0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    with urllib.request.urlopen(item["url"], timeout=120) as r:
        return r.read()


def gen_gemini(prompt: str, portrait: bool, model: str, key: str) -> bytes:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"],
                                 "imageConfig": {"aspectRatio": "9:16" if portrait else "16:9"}}}
    data = http_json(url, body, {"x-goog-api-key": key}, timeout=300)
    for part in data["candidates"][0]["content"]["parts"]:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline:
            return base64.b64decode(inline["data"])
    raise RuntimeError("Gemini 응답에 이미지가 없습니다.")


def generate_illustrations(plan: dict, out_dir: Path, portrait: bool, args) -> None:
    ills = plan["illustrations"]
    if not ills:
        return
    img_dir = out_dir / "illustrations"
    img_dir.mkdir(exist_ok=True)
    okey, gkey = os.environ.get("OPENAI_API_KEY", ""), os.environ.get("GEMINI_API_KEY", "")
    provider = args.image_provider
    if provider == "auto":
        provider = "openai" if okey else "gemini" if gkey else "none"
    for it in ills:
        path = img_dir / it["image"]
        if path.exists():
            continue                                   # 이미 있으면 재사용(직접 바꿔 넣은 그림 포함)
        if provider == "none":
            log(f"   [건너뜀] 이미지 API 키가 없어 {it['image']} 을(를) 만들지 못했습니다. "
                f"(OPENAI_API_KEY 또는 GEMINI_API_KEY 설정, 또는 직접 그림을 넣어 주세요)")
            continue
        full = f"{it['prompt'].rstrip('.')}. {STYLE}"
        log(f"   🎨 {it['image']} 그리는 중 ({provider})… {it['prompt'][:60]}")
        try:
            if provider == "openai":
                png = gen_openai(full, portrait, args.openai_model, okey)
            else:
                png = gen_gemini(full, portrait, args.gemini_model, gkey)
            path.write_bytes(png)
        except Exception as e:
            log(f"   [경고] {it['image']} 생성 실패: {e}")


def prepare_card(img_path: Path, label: str, W: int, H: int, mode: str, font_path: str | None):
    """설명 화면 한 장을 미리 합성. full: (H,W,3) / overlay: (h,w,4) BGRA."""
    import cv2
    from PIL import Image, ImageDraw, ImageFont
    img = Image.open(img_path).convert("RGB")

    def cover(im, tw, th):
        s = max(tw / im.width, th / im.height)
        im = im.resize((max(tw, int(im.width * s + 0.5)), max(th, int(im.height * s + 0.5))), Image.LANCZOS)
        x, y = (im.width - tw) // 2, (im.height - th) // 2
        return im.crop((x, y, x + tw, y + th))

    if mode == "full":
        # 줌 여유분 8% 를 두고 크게 만들어 둠
        base = cover(img, int(W * 1.08), int(H * 1.08))
        canvas = base
        cw, ch = canvas.size
        scale_ref = min(W, H)
    else:
        cw = int(min(W, H) * 0.5)
        ch = int(cw * img.height / img.width)
        pad = max(6, cw // 40)
        card = Image.new("RGBA", (cw + pad * 2, ch + pad * 2), (255, 255, 255, 255))
        card.paste(img.resize((cw, ch), Image.LANCZOS), (pad, pad))
        canvas = card
        cw, ch = canvas.size
        scale_ref = cw

    if label:
        d = ImageDraw.Draw(canvas)
        size = max(18, int(scale_ref * (0.075 if mode == "full" else 0.09)))
        try:
            font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()
        bx = d.textbbox((0, 0), label, font=font)
        tw, th = bx[2] - bx[0], bx[3] - bx[1]
        px, py = int(size * 0.6), int(size * 0.35)
        x = (cw - tw) // 2
        y = ch - th - py * 2 - int(ch * (0.1 if mode == "full" else 0.05))
        d.rounded_rectangle((x - px, y - py, x + tw + px, y + th + py * 2), radius=int(size * 0.6),
                            fill=(255, 253, 245) if mode == "full" else (40, 40, 40),
                            outline=(30, 30, 30), width=max(2, size // 12))
        d.text((x - bx[0], y - bx[1] + py // 2), label, font=font,
               fill=(30, 30, 30) if mode == "full" else (255, 255, 255))

    arr = np.array(canvas)
    if arr.shape[2] == 4:
        return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGRA)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def ease(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def apply_card(frame: np.ndarray, card: np.ndarray, t: float, dur: float, mode: str) -> np.ndarray:
    import cv2
    H, W = frame.shape[:2]
    fade = 0.25
    a = ease(t / fade) * ease((dur - t) / fade)
    if mode == "full":
        ch, cw = card.shape[:2]
        z = 1.0 + 0.06 * (t / max(dur, 0.1))           # 천천히 살짝 확대 (켄 번즈)
        vw, vh = int(cw / 1.08 / z), int(ch / 1.08 / z)   # 8% 여유 안에서 점점 좁게 보기 = 확대
        x0, y0 = (cw - vw) // 2, (ch - vh) // 2
        view = cv2.resize(card[y0:y0 + vh, x0:x0 + vw], (W, H), interpolation=cv2.INTER_AREA)
        if a >= 0.999:
            return view
        return cv2.addWeighted(view, a, frame, 1 - a, 0)
    # overlay: 오른쪽 위 카드 + 살짝 떠오르는 움직임 + 그림자
    ch, cw = card.shape[:2]
    margin = int(min(W, H) * 0.04)
    x = W - cw - margin
    y = margin + int((1 - ease(t / 0.35)) * margin)
    if x < 0 or y + ch > H:
        s = min((W - 2 * margin) / cw, (H - 2 * margin) / ch, 1)
        card = cv2.resize(card, (int(cw * s), int(ch * s)))
        ch, cw = card.shape[:2]
        x, y = W - cw - margin, margin
    out = frame.copy()
    sh = int(margin * 0.3)
    ys, xs = slice(y + sh, min(H, y + sh + ch)), slice(x + sh, min(W, x + sh + cw))
    out[ys, xs] = (out[ys, xs] * (1 - 0.35 * a)).astype(np.uint8)
    roi = out[y:y + ch, x:x + cw].astype(np.float32)
    alpha = card[:, :, 3:4].astype(np.float32) / 255 * a
    out[y:y + ch, x:x + cw] = (card[:, :, :3] * alpha + roi * (1 - alpha)).astype(np.uint8)
    return out


# ─────────────────────────────────────────────────────────────
# 7) 화면 속 개인정보(OCR) + 얼굴
# ─────────────────────────────────────────────────────────────
PII_SCREEN_EXTRA = {
    "주소": r"([가-힣]+(시|도)\s?[가-힣]+(구|군|시))|([가-힣0-9]+(로|길)\s?\d+(-\d+)?)|(\d+동\s?\d+호)",
}


def screen_pii(text: str) -> str | None:
    t = text.strip()
    for kind, pat in {**PII_SCREEN_EXTRA, **PII_TEXT_PATTERNS}.items():
        if re.search(pat, t):
            return kind
    if len(re.sub(r"\D", "", t)) >= 9 and re.fullmatch(r"[\d\s\-().*]+", t):
        return "숫자열"
    return None


def ocr_scan(video: Path, info: dict, frame_iv: list, interval: float) -> list[dict]:
    """[{t0, t1, box:(x0,y0,x1,y1) 원본 좌표, type, text}]"""
    try:
        import easyocr
    except ImportError:
        log("   [안내] easyocr 가 없어 화면 글자 개인정보 검사를 건너뜁니다. (pip install easyocr)")
        return []
    log("▶ 화면 속 글자에서 개인정보 찾는 중(OCR)…")
    reader = easyocr.Reader(["ko", "en"], gpu=False, verbose=False)
    W, H = info["w"], info["h"]
    scale = min(1.0, 1280 / max(W, H))
    sw, shh = int(W * scale) // 2 * 2, int(H * scale) // 2 * 2
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(video),
                             "-vf", f"fps=1/{interval},scale={sw}:{shh}",
                             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    fps = info["fps"]
    events, k = [], 0
    while True:
        buf = proc.stdout.read(sw * shh * 3)
        if len(buf) < sw * shh * 3:
            break
        t = k * interval
        k += 1
        f = t * fps
        if not any(a - fps * interval <= f < b + fps * interval for a, b in frame_iv):
            continue                                   # 어차피 잘려나갈 구간
        img = np.frombuffer(buf, np.uint8).reshape(shh, sw, 3)
        for box, text, conf in reader.readtext(img):
            kind = screen_pii(text)
            if kind and conf > 0.25:
                xs = [p[0] / scale for p in box]
                ys = [p[1] / scale for p in box]
                events.append({"t0": max(0, t - interval * 0.75), "t1": t + interval * 0.75,
                               "box": (min(xs), min(ys), max(xs), max(ys)),
                               "type": kind, "text": text})
                log(f"   {fmt_ts(t)}  {kind}: {text}")
    proc.wait()
    return events


def pixelate(frame: np.ndarray, x0, y0, x1, y1, grow: float = 0.15) -> None:
    import cv2
    H, W = frame.shape[:2]
    gw, gh = (x1 - x0) * grow, (y1 - y0) * grow
    x0, y0 = int(max(0, x0 - gw)), int(max(0, y0 - gh))
    x1, y1 = int(min(W, x1 + gw)), int(min(H, y1 + gh))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return
    roi = frame[y0:y1, x0:x1]
    block = max(6, min(x1 - x0, y1 - y0) // 6)
    small = cv2.resize(roi, (max(1, (x1 - x0) // block), max(1, (y1 - y0) // block)),
                       interpolation=cv2.INTER_LINEAR)
    frame[y0:y1, x0:x1] = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)


class FaceBlur:
    YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
                 "face_detection_yunet_2023mar.onnx")

    def __init__(self, W: int, H: int, every: int = 3):
        import cv2
        self.cv2, self.every, self.boxes, self.n = cv2, every, [], 0
        self.scale = min(1.0, 640 / max(W, H))
        self.sw, self.sh = int(W * self.scale), int(H * self.scale)
        self.yunet = None
        model = Path(__file__).with_name("face_detection_yunet.onnx")
        try:
            if not model.exists():
                log("   얼굴 인식 모델 내려받는 중…")
                urllib.request.urlretrieve(self.YUNET_URL, model)
            self.yunet = cv2.FaceDetectorYN.create(str(model), "", (self.sw, self.sh), 0.6)
        except Exception as e:
            log(f"   [안내] YuNet 사용 불가({e}) → 기본 얼굴 인식 사용")
            xml = Path(getattr(getattr(cv2, "data", None), "haarcascades", "")) \
                / "haarcascade_frontalface_default.xml"
            self.haar = cv2.CascadeClassifier(str(xml)) if xml.exists() else None
            if self.haar is None or self.haar.empty():
                log("   [경고] 얼굴 인식 모델이 없어 얼굴 모자이크를 할 수 없습니다. "
                    "(인터넷 연결 후 다시 실행하거나 pip install opencv-python)")
                self.haar = None

    def __call__(self, frame: np.ndarray) -> None:
        if self.n % self.every == 0:
            small = self.cv2.resize(frame, (self.sw, self.sh))
            if self.yunet is not None:
                _, faces = self.yunet.detect(small)
                faces = [] if faces is None else [f[:4] for f in faces]
            elif self.haar is None:
                faces = []
            else:
                g = self.cv2.cvtColor(small, self.cv2.COLOR_BGR2GRAY)
                faces = list(self.haar.detectMultiScale(g, 1.1, 5, minSize=(24, 24)))
            self.boxes = [(x / self.scale, y / self.scale, (x + w) / self.scale, (y + h) / self.scale)
                          for x, y, w, h in faces]
        self.n += 1
        for b in self.boxes:
            pixelate(frame, *b, grow=0.3)


# ─────────────────────────────────────────────────────────────
# 8) 오디오 조립 (삐 처리 + 컷 이어 붙이기)
# ─────────────────────────────────────────────────────────────
def build_audio(audio: np.ndarray, frame_iv: list, fps: float, beeps: list) -> np.ndarray:
    a = audio.copy()
    for s, e in beeps:
        i0, i1 = int(s * SR), min(len(a), int(e * SR))
        if i1 > i0:
            t = np.arange(i1 - i0) / SR
            tone = (0.12 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
            ramp = np.minimum(1, np.minimum(np.arange(i1 - i0), np.arange(i1 - i0)[::-1]) / (SR * 0.005))
            a[i0:i1] = (tone * ramp)[:, None]
    pieces, fade = [], int(SR * 0.008)
    for fa, fb in frame_iv:
        i0, i1 = int(round(fa / fps * SR)), int(round(fb / fps * SR))
        seg = np.zeros((i1 - i0, 2), np.float32)
        src = a[i0:min(i1, len(a))]
        seg[:len(src)] = src
        if len(seg) > 2 * fade:                       # 이음새 '딱' 소리 방지
            r = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
            seg[:fade] *= r
            seg[-fade:] *= r[::-1]
        pieces.append(seg)
    return np.concatenate(pieces) if pieces else np.zeros((1, 2), np.float32)


def write_wav(path: Path, a: np.ndarray) -> None:
    import wave
    pcm = (np.clip(a, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


# ─────────────────────────────────────────────────────────────
# 9) 렌더링
# ─────────────────────────────────────────────────────────────
def render(video: Path, info: dict, frame_iv: list, audio_out: np.ndarray, cards: list,
           ocr_events: list, face_blur: bool, out_path: Path, crf: int, mode: str) -> None:
    W, H, fps = info["w"], info["h"], info["fps"]
    W2, H2 = W - W % 2, H - H % 2
    keep = np.zeros(int(round(info["duration"] * fps)) + 2, bool)
    for a, b in frame_iv:
        keep[a:b] = True
    total_out = int(sum(b - a for a, b in frame_iv))
    faces = FaceBlur(W, H) if face_blur else None

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td, "audio.wav")
        write_wav(wav, audio_out)
        dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(video.resolve()),
                                "-vf", f"fps={fps}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                               stdout=subprocess.PIPE)
        enc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W2}x{H2}", "-r", f"{fps}", "-i", "-",
             "-i", str(wav), "-map", "0:v", "-map", "1:a",
             "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
             "-movflags", "+faststart", "-shortest", str(out_path.resolve())],
            stdin=subprocess.PIPE)
        fsize, i, k = W * H * 3, 0, 0
        last_pct = -1
        while True:
            buf = dec.stdout.read(fsize)
            if len(buf) < fsize:
                break
            if i < len(keep) and keep[i]:
                t_src, t_out = i / fps, k / fps
                frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3)[:H2, :W2].copy()
                for ev in ocr_events:
                    if ev["t0"] <= t_src <= ev["t1"]:
                        pixelate(frame, *ev["box"])
                if faces:
                    faces(frame)
                for c in cards:
                    if c["t0"] <= t_out < c["t0"] + c["dur"]:
                        frame = apply_card(frame, c["img"], t_out - c["t0"], c["dur"], mode)
                enc.stdin.write(frame.tobytes())
                k += 1
                pct = int(k * 100 / max(total_out, 1))
                if pct // 10 != last_pct // 10:
                    log(f"   렌더링 {pct}%")
                    last_pct = pct
            i += 1
        dec.wait()
        enc.stdin.close()
        if enc.wait() != 0:
            raise RuntimeError("ffmpeg 인코딩 실패")


# ─────────────────────────────────────────────────────────────
# 메인 처리
# ─────────────────────────────────────────────────────────────
def process(video: Path, args) -> Path:
    info = probe(video)
    out_dir = args.out or video.with_name(video.stem + "_edited")
    out_dir.mkdir(parents=True, exist_ok=True)
    W, H, fps = info["w"], info["h"], info["fps"]
    portrait = H > W
    log(f"▶ 원본 {W}x{H} · {fps:.2f}fps · {fmt_ts(info['duration'])}")

    # 1) 대본
    tr_path = out_dir / "transcript.json"
    if tr_path.exists():
        log(f"▶ 저장된 대본 사용: {tr_path.name}")
        tr = json.loads(tr_path.read_text(encoding="utf-8"))
    else:
        tr = transcribe(video, args.whisper_model, args.language)
        tr_path.write_text(json.dumps(tr, ensure_ascii=False, indent=1), encoding="utf-8")
    words = [w for w in tr["words"] if w["end"] > w["start"]]
    if not words:
        sys.exit("[오류] 인식된 말이 없습니다. 말소리가 있는 영상인지 확인해 주세요.")
    sents = build_sentences(words)
    cands = filler_candidates(words)
    log(f"▶ 단어 {len(words)}개 · 문장 {len(sents)}개 · 필러 후보 {len(cands)}개")

    # 2) 계획
    max_ill = args.max_illustrations
    if max_ill < 0:
        max_ill = int(min(12, max(1, round(info["duration"] / 40))))
    plan_path = out_dir / "plan.json"
    if plan_path.exists() and not args.replan:
        log(f"▶ 저장된 편집 계획 사용: {plan_path.name}  (새로 짜려면 --replan)")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    else:
        plan = make_plan(words, sents, cands, args, 0 if args.no_illustrations else max_ill)
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.no_illustrations:
        plan["illustrations"] = []

    # 3) 컷
    audio = load_audio(video, info["audio"], info["duration"])
    keep, stats = compute_keep(words, sents, plan["fillers_cut"], audio, info["duration"], args)
    n_frames = int(info["duration"] * fps)
    frame_iv = snap_to_frames(keep, fps, n_frames)
    if not frame_iv:
        sys.exit("[오류] 남길 구간이 없습니다. --max-pause 를 늘려 보세요.")
    tm = TimeMap(frame_iv, fps)
    speech = sum(w["end"] - w["start"] for w in words)
    if speech < info["duration"] * 0.25:
        log("   [주의] 말소리가 영상의 25% 미만입니다. 말 없는 장면(풍경·행동)이 많이 잘릴 수 있어요.")
    log(f"▶ 컷 편집: {fmt_ts(info['duration'])} → {fmt_ts(tm.out_duration)} "
        f"(구간 {len(frame_iv)}개, 필러 {len(plan['fillers_cut'])}개 제거)")

    # 4) 설명 화면
    cards = []
    if plan["illustrations"]:
        log("▶ 설명 화면 준비 중…")
        generate_illustrations(plan, out_dir, portrait, args)
        font = find_font()
        last_end = -99.0
        for it in plan["illustrations"]:
            p = out_dir / "illustrations" / it["image"]
            if not p.exists():
                continue
            s = sents[it["sentence"]]
            t0 = tm.to_out(s.start) + 0.15
            dur = min(it["duration"], tm.out_duration - t0)
            if dur < 1.5 or t0 < last_end + 2:
                continue
            cards.append({"t0": t0, "dur": dur, "img": prepare_card(p, it.get("label", ""), W - W % 2,
                                                                    H - H % 2, args.illustration_mode, font),
                          "meta": it})
            it["out_time"] = round(t0, 2)
            last_end = t0 + dur

    # 5) 개인정보
    beeps = []
    for p in plan["pii_spoken"]:
        ws = [words[k] for k in p["wids"] if k < len(words)]
        if ws:
            beeps.append((ws[0]["start"] - 0.03, ws[-1]["end"] + 0.03))
    ocr_events = [] if args.no_ocr else ocr_scan(video, info, frame_iv, args.ocr_interval)

    # 6) 렌더링
    audio_out = build_audio(audio, frame_iv, fps, beeps)
    out_path = out_dir / f"{video.stem}_edited.mp4"
    log("▶ 렌더링 중…")
    render(video, info, frame_iv, audio_out, cards, ocr_events, args.blur_faces,
           out_path, args.crf, args.illustration_mode)

    # 7) 보고서
    write_report(out_dir, video, info, tm, words, sents, plan, cards, ocr_events, stats, args)
    log(f"\n완료! 편집본: {out_path}")
    return out_path


def write_report(out_dir, video, info, tm, words, sents, plan, cards, ocr_events, stats, args):
    md = [f"# {video.name} 편집 보고서\n",
          f"- 원본 길이: {fmt_ts(info['duration'])} → 편집본: {fmt_ts(tm.out_duration)} "
          f"({info['duration'] - tm.out_duration:.1f}초 줄임)",
          f"- 계획 방식: {'Claude' if plan.get('planner') == 'claude' else '규칙 기반'} · "
          f"무음 기준 {stats['silence_threshold_db']} dB · 긴 쉼 기준 {args.max_pause}초",
          f"- 필러 제거 {len(plan['fillers_cut'])}개 · 설명 화면 {len(cards)}개 · "
          f"말 개인정보 삐 {len(plan['pii_spoken'])}곳 · 화면 개인정보 모자이크 {len(ocr_events)}건"
          + (" · 얼굴 모자이크 켬" if args.blur_faces else ""), ""]
    if plan["fillers_cut"]:
        md.append("## 지운 필러")
        for k in plan["fillers_cut"][:200]:
            ctx = "".join(words[j]["word"] for j in range(max(0, k - 3), min(len(words), k + 4)))
            md.append(f"- {fmt_ts(words[k]['start'])} **{words[k]['word'].strip()}**  …{ctx.strip()}…")
        md.append("")
    if cards:
        md.append("## 설명 화면")
        for c in cards:
            it = c["meta"]
            md += [f"### {fmt_ts(c['t0'])} (편집본 기준, {c['dur']:.1f}초) — {it['image']}",
                   f"- 대사: {sents[it['sentence']].text}",
                   f"- 키워드: {it.get('label') or '(없음)'}",
                   f"- 이유: {it.get('why', '')}",
                   f"- 그림 지시: {it['prompt']}", ""]
    if plan["pii_spoken"]:
        md.append("## 말로 나온 개인정보 (삐 처리)")
        for p in plan["pii_spoken"]:
            md.append(f"- {fmt_ts(tm.to_out(words[p['wids'][0]]['start']))} {p['type']}: {p['text']}")
        md.append("")
    if ocr_events:
        md.append("## 화면 속 개인정보 (모자이크)")
        for e in ocr_events:
            md.append(f"- 원본 {fmt_ts(e['t0'])}~{fmt_ts(e['t1'])} {e['type']}: {e['text']}")
        md.append("")
    md += ["## 고치는 방법",
           "- `plan.json` 의 `fillers_cut`(지울 필러 번호), `illustrations`(설명 화면) 을 고친 뒤 다시 실행하면 반영됩니다.",
           "- `illustrations/` 폴더의 그림을 같은 이름으로 바꿔 넣으면 그 그림이 쓰입니다. 지우면 새로 그립니다.",
           "- 처음부터 다시 계획하려면 `--replan` 을 붙이세요."]
    (out_dir / "report.md").write_text("\n".join(md), encoding="utf-8")
    plan_path = out_dir / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────────────────────
# 폴더 감시 모드
# ─────────────────────────────────────────────────────────────
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}


def watch(folder: Path, args) -> None:
    log(f"▶ '{folder}' 폴더를 지켜보는 중… 영상을 넣으면 자동으로 편집합니다. (끝내려면 Ctrl+C)")
    sizes: dict[Path, int] = {}
    while True:
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() not in VIDEO_EXT or f.stem.endswith("_edited"):
                continue
            done = f.with_name(f.stem + "_edited") / f"{f.stem}_edited.mp4"
            if done.exists():
                continue
            size = f.stat().st_size
            if sizes.get(f) != size:          # 아직 복사 중이면 다음 바퀴에 다시 확인
                sizes[f] = size
                continue
            log(f"\n━━━ 새 영상: {f.name} ━━━")
            try:
                process(f, args)
            except SystemExit as e:
                log(str(e))
            except Exception as e:
                log(f"[오류] {f.name}: {e}")
        time.sleep(10)


def main() -> None:
    ap = argparse.ArgumentParser(description="무음·필러 컷 + 설명 일러스트 + 개인정보 모자이크 자동 편집")
    ap.add_argument("video", type=Path, nargs="?", help="원본 영상 파일")
    ap.add_argument("--watch", type=Path, help="이 폴더에 들어오는 영상을 자동 처리")
    ap.add_argument("--out", type=Path, help="출력 폴더 (기본: <영상이름>_edited)")
    # 컷
    ap.add_argument("--max-pause", type=float, default=0.45, help="이보다 긴 쉼은 잘라냄(초, 기본 0.45)")
    ap.add_argument("--pad", type=float, default=0.06, help="단어 앞뒤 여유(초)")
    ap.add_argument("--sentence-breath", type=float, default=0.14, help="문장 끝에 남길 숨 고르기(초)")
    ap.add_argument("--silence-db", type=float, help="무음 기준 dB (기본: 자동)")
    # 계획·인식
    ap.add_argument("--model", default=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"), help="Claude 모델 ID")
    ap.add_argument("--no-claude", action="store_true", help="Claude 를 쓰지 않음(규칙 기반)")
    ap.add_argument("--replan", action="store_true", help="저장된 plan.json 무시하고 다시 계획")
    ap.add_argument("--whisper-model", default="small", help="tiny/base/small/medium/large-v3")
    ap.add_argument("--language", default="ko")
    # 설명 화면
    ap.add_argument("--max-illustrations", type=int, default=-1, help="설명 화면 최대 개수 (기본: 40초당 1개)")
    ap.add_argument("--no-illustrations", action="store_true", help="설명 화면 넣지 않기")
    ap.add_argument("--illustration-mode", choices=["full", "overlay"], default="full",
                    help="full: 화면 전체 컷인 / overlay: 오른쪽 위 카드")
    ap.add_argument("--image-provider", choices=["auto", "openai", "gemini", "none"], default="auto")
    ap.add_argument("--openai-model", default=os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1"))
    ap.add_argument("--gemini-model", default=os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image"))
    # 개인정보
    ap.add_argument("--blur-faces", action="store_true", help="등장하는 얼굴 모두 모자이크")
    ap.add_argument("--no-ocr", action="store_true", help="화면 글자 개인정보 검사 끄기")
    ap.add_argument("--ocr-interval", type=float, default=1.0, help="OCR 검사 간격(초)")
    ap.add_argument("--crf", type=int, default=20, help="화질(낮을수록 고화질, 18~23)")
    args = ap.parse_args()

    need("ffmpeg"); need("ffprobe")
    if args.watch:
        args.out = None
        watch(args.watch, args)
    elif args.video:
        if not args.video.exists():
            sys.exit(f"[오류] 파일이 없습니다: {args.video}")
        process(args.video, args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
