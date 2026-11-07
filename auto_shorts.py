#!/usr/bin/env python3
"""
auto_shorts.py — 긴 영상에서 핵심 구간을 골라 세로 쇼츠(1080x1920)로 자동 생성

하는 일
  1) 음성 인식(faster-whisper)으로 단어 단위 대본 만들기
  2) 핵심 구간 고르기
     - Claude API 키가 있으면: 대본을 읽고 "앞뒤 맥락 없이도 이해되고, 첫 문장이 훅이 되는"
       구간을 골라 화면 제목·업로드 제목·설명·해시태그까지 만들어 줌
     - 키가 없으면: 말 속도, 목소리 크기, 핵심 단어(방법·꿀팁·실수·돈…), 질문형 시작 등으로 점수를 매겨 고름
  3) 세로 영상 만들기
     - blur : 원본을 가운데에 두고 위아래는 흐린 배경 (화면 녹화·PPT 강의에 적합)
     - crop : 얼굴을 따라가며 세로로 잘라냄 (얼굴이 나오는 토크 영상에 적합)
     - auto : 얼굴이 자주 보이면 crop, 아니면 blur (기본)
  4) 쇼츠용 다듬기: 구간 안의 긴 쉼 자르기, 큰 자막, 상단 제목, 음량 맞추기
  5) 결과: <영상이름>_shorts/ 폴더에 short_01.mp4 … + 업로드용 정리(shorts_report.md) + 계획(shorts_plan.json)

다시 실행하면 대본과 계획을 재사용합니다. shorts_plan.json 의 시작·끝 시간이나 제목을 고치고
다시 실행하면 그대로 반영됩니다.

사용 예
  python auto_shorts.py 내영상.mp4
  python auto_shorts.py 내영상.mp4 --count 5
  python auto_shorts.py 내영상.mp4 --layout crop
  python auto_shorts.py 내영상.mp4 --only 2          # 2번 쇼츠만 다시 렌더링
  python auto_shorts.py --watch 쇼츠                  # 폴더에 영상이 들어오면 자동 처리
"""
from __future__ import annotations

import argparse
import json
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

SR = 48000                       # 작업용 오디오 샘플레이트
OW, OH = 1080, 1920              # 출력 해상도 (세로)
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}


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
         "stream=codec_type,width,height,r_frame_rate,avg_frame_rate:stream_tags=rotate"
         ":stream_side_data=rotation:format=duration",
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
    fps = min(fps, 60.0)
    w, h = int(v["width"]), int(v["height"])
    # 휴대폰 영상의 회전 정보 반영 (ffmpeg 는 자동으로 돌려서 디코딩함)
    rot = 0
    try:
        rot = int(float(v.get("tags", {}).get("rotate", 0)))
    except Exception:
        pass
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                rot = int(float(sd["rotation"]))
            except Exception:
                pass
    if abs(rot) % 180 == 90:
        w, h = h, w
    return {"w": w, "h": h, "fps": fps,
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


def find_font() -> str | None:
    cands = {
        "Windows": [r"C:\Windows\Fonts\malgunbd.ttf", r"C:\Windows\Fonts\malgun.ttf"],
        "Darwin": ["/System/Library/Fonts/AppleSDGothicNeo.ttc"],
    }.get(platform.system(), []) + [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
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
def transcribe(video: Path, model_size: str, language: str) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("[오류] faster-whisper 가 없습니다:  pip install faster-whisper")
    log(f"▶ 음성 인식 중… (모델: {model_size}, 영상 길이에 따라 수 분 걸립니다)")
    model = WhisperModel(model_size, device="auto", compute_type="auto")
    segments, _ = model.transcribe(
        str(video), language=language, word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300}, beam_size=5)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word})
        log(f"   {fmt_ts(seg.end)}  {seg.text.strip()[:60]}")
    return {"language": language, "words": words}


# ─────────────────────────────────────────────────────────────
# 2) 문장 묶기
# ─────────────────────────────────────────────────────────────
@dataclass
class Sentence:
    idx: int
    start: float
    end: float
    text: str
    wids: list = field(default_factory=list)


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


# ─────────────────────────────────────────────────────────────
# 3) 핵심 구간 고르기
# ─────────────────────────────────────────────────────────────
CLAUDE_PROMPT = """당신은 유튜브 쇼츠 편집자입니다. 아래는 긴 영상의 대본입니다.
각 줄은 [문장번호] (시작시간) 문장 형식입니다.

이 영상에서 쇼츠로 잘라 올리기 좋은 구간을 최대 {count}개 골라 주세요.

좋은 쇼츠 구간의 조건
- 길이: {min_len:.0f}~{max_len:.0f}초 (문장 시작시간으로 계산)
- 앞뒤 맥락 없이 이 구간만 봐도 이해되고, 하나의 메시지로 끝나야 합니다.
- 첫 문장이 훅이어야 합니다: 질문, 놀라운 사실, 숫자, 흔한 실수, 결과 먼저 말하기 등.
  "그래서", "아까 말한 것처럼"처럼 앞 내용을 가리키는 말로 시작하는 구간은 피하세요.
- 구간끼리 겹치면 안 됩니다. 점수(1~10)가 높은 순서로 적어 주세요.
- 좋은 구간이 {count}개보다 적으면 있는 만큼만 고르세요.

각 구간마다 만들 것
- start_sentence, end_sentence: 포함할 첫 문장과 마지막 문장 번호
- screen_title: 영상 위쪽에 계속 띄울 제목. 16자 이내, 궁금하게 만드는 문장
- upload_title: 유튜브 업로드 제목. 40자 이내, 끝에 #shorts 는 붙이지 마세요
- description: 업로드 설명란 2~3문장
- hashtags: 3~5개, 각각 # 로 시작
- reason: 이 구간을 고른 이유 한 줄
- score: 1~10

반드시 아래 JSON 하나만 출력하세요. 다른 말은 쓰지 마세요.
{{"shorts": [{{"start_sentence": 0, "end_sentence": 5, "screen_title": "", "upload_title": "",
  "description": "", "hashtags": ["#"], "reason": "", "score": 8}}]}}

대본:
{transcript}
"""


def ask_claude(sents: list[Sentence], args) -> list[dict]:
    key = os.environ["ANTHROPIC_API_KEY"]
    transcript = "\n".join(f"[{s.idx}] ({fmt_ts(s.start)}) {s.text}" for s in sents)
    prompt = CLAUDE_PROMPT.format(count=args.count, min_len=args.min_len,
                                  max_len=args.max_len, transcript=transcript)
    data = http_json("https://api.anthropic.com/v1/messages",
                     {"model": args.model, "max_tokens": 6000,
                      "messages": [{"role": "user", "content": prompt}]},
                     {"x-api-key": key, "anthropic-version": "2023-06-01"})
    text = "".join(b.get("text", "") for b in data.get("content", []))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("응답에서 JSON 을 찾지 못했습니다: " + text[:200])
    out = []
    for it in json.loads(m.group(0)).get("shorts", []):
        try:
            a, b = int(it["start_sentence"]), int(it["end_sentence"])
            assert 0 <= a <= b < len(sents)
        except Exception:
            continue
        out.append({"a": a, "b": b,
                    "title": str(it.get("screen_title", ""))[:24],
                    "upload_title": str(it.get("upload_title", "")),
                    "description": str(it.get("description", "")),
                    "hashtags": [str(h) for h in it.get("hashtags", [])][:6],
                    "reason": str(it.get("reason", "")),
                    "score": float(it.get("score", 5))})
    return out


HOOK_WORDS = ["방법", "비법", "꿀팁", "팁", "핵심", "중요", "비밀", "실수", "절대", "무조건", "꼭",
              "진짜", "사실", "이유", "돈", "수익", "월", "만원", "억", "처음", "제일", "가장",
              "결론", "정리", "추천", "주의", "망", "성공", "실패", "왜", "어떻게", "얼마"]
BAD_START = re.compile(r"^(그래서|그리고|그러니까|아까|이게|그게|그거|그럼|근데|그런데|또|그래가지고)")


def sentence_loudness(video: Path, info: dict, sents: list[Sentence]) -> np.ndarray:
    """문장별 평균 음량(dB). 긴 영상도 메모리를 적게 쓰도록 8kHz 모노로 읽음."""
    if not info["audio"]:
        return np.zeros(len(sents))
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "8000",
                          "-f", "f32le", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.float32)
    out = np.zeros(len(sents))
    for i, s in enumerate(sents):
        seg = a[int(s.start * 8000): int(s.end * 8000)]
        out[i] = 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-9) if len(seg) else -60
    return out


def pick_local(video: Path, info: dict, sents: list[Sentence], args) -> list[dict]:
    """API 키 없이 규칙으로 고르기."""
    if not sents:
        return []
    loud = sentence_loudness(video, info, sents)
    lz = (loud - loud.mean()) / (loud.std() + 1e-6)

    def s_score(s: Sentence) -> float:
        t = s.text
        sc = sum(1.0 for w in HOOK_WORDS if w in t)
        sc += 1.0 if re.search(r"\d", t) else 0
        sc += 0.8 if "?" in t or re.search(r"(까요|나요|죠)\??$", t) else 0
        return sc

    base = np.array([s_score(s) for s in sents])
    target = (args.min_len + args.max_len) / 2
    cands = []
    for i in range(len(sents)):
        j, dur = i, 0.0
        while j < len(sents):
            dur = sents[j].end - sents[i].start
            if dur >= target or (j + 1 < len(sents) and sents[j + 1].end - sents[i].start > args.max_len):
                break
            j += 1
        j = min(j, len(sents) - 1)
        dur = sents[j].end - sents[i].start
        if dur < args.min_len or dur > args.max_len + 5:
            continue
        idx = range(i, j + 1)
        talk = sum(sents[k].end - sents[k].start for k in idx) / max(dur, 1e-6)   # 말 비율
        chars = sum(len(sents[k].text) for k in idx) / max(dur, 1e-6)              # 말 속도
        score = (base[i:j + 1].mean() * 2.0 + base[i] * 1.5      # 핵심 단어, 첫 문장 훅
                 + lz[i:j + 1].mean() * 0.8 + talk * 2.0 + min(chars, 8) * 0.15
                 - (2.5 if BAD_START.match(sents[i].text) else 0)
                 + (1.0 if END_KO.search(sents[j].text) or END_PUNCT.search(sents[j].text) else 0))
        cands.append((score, i, j))
    cands.sort(reverse=True)
    chosen = []
    for score, i, j in cands:
        if any(not (j < a or i > b) for _, a, b in chosen):
            continue
        chosen.append((score, i, j))
        if len(chosen) >= args.count:
            break
    out = []
    for score, i, j in chosen:
        first = sents[i].text
        title = first if len(first) <= 16 else first[:15].rstrip() + "…"
        out.append({"a": i, "b": j, "title": title, "upload_title": first[:40],
                    "description": " ".join(sents[k].text for k in range(i, min(j + 1, i + 2))),
                    "hashtags": ["#shorts"], "reason": "규칙 기반(핵심 단어·말 속도·음량)",
                    "score": round(float(score), 2)})
    return out


def fit_range(sents: list[Sentence], a: int, b: int, args) -> tuple[int, int]:
    """문장 범위를 최소~최대 길이에 맞춘다."""
    while b > a and sents[b].end - sents[a].start > args.max_len:
        b -= 1
    while (sents[b].end - sents[a].start < args.min_len and b + 1 < len(sents)
           and sents[b + 1].end - sents[a].start <= args.max_len
           and sents[b + 1].start - sents[b].end < 2.0):
        b += 1
    return a, b


def make_plan(video: Path, info: dict, sents: list[Sentence], args) -> dict:
    picks, planner = None, "local"
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key and not args.no_claude:
        log(f"▶ Claude({args.model})가 대본을 읽고 쇼츠 구간을 고르는 중…")
        try:
            picks, planner = ask_claude(sents, args), "claude"
        except Exception as e:
            log(f"   Claude 호출 실패({e}) → 규칙 기반으로 고릅니다")
    elif not key:
        log("   [안내] ANTHROPIC_API_KEY 가 없어 규칙 기반으로 구간을 고릅니다. (제목·설명 품질은 낮아요)")
    if not picks:
        log("▶ 규칙 기반으로 쇼츠 구간을 고르는 중…")
        picks = pick_local(video, info, sents, args)

    picks.sort(key=lambda p: -p["score"])
    shorts, used = [], []
    for p in picks:
        a, b = fit_range(sents, p["a"], p["b"], args)
        if any(not (b < x or a > y) for x, y in used):
            continue
        used.append((a, b))
        t0 = max(0.0, sents[a].start - 0.15)
        t1 = min(info["duration"], sents[b].end + 0.30)
        tags = [h if h.startswith("#") else "#" + h for h in p["hashtags"]]
        if "#shorts" not in [t.lower() for t in tags]:
            tags.append("#shorts")
        shorts.append({"id": len(shorts) + 1, "start": round(t0, 2), "end": round(t1, 2),
                       "title": p["title"], "upload_title": p["upload_title"],
                       "description": p["description"], "hashtags": tags,
                       "reason": p["reason"], "score": p["score"],
                       "first_line": sents[a].text})
        if len(shorts) >= args.count:
            break
    return {"source": video.name, "planner": planner, "shorts": shorts}


# ─────────────────────────────────────────────────────────────
# 4) 구간 안 편집: 긴 쉼 자르기
# ─────────────────────────────────────────────────────────────
def keep_intervals(words: list, t0: float, t1: float, args) -> list[tuple[float, float]]:
    if args.no_trim:
        return [(t0, t1)]
    ws = [w for w in words if w["end"] > t0 and w["start"] < t1]
    if not ws:
        return [(t0, t1)]
    pad = 0.12
    iv = [(max(t0, w["start"] - pad), min(t1, w["end"] + pad)) for w in ws]
    iv[0] = (t0, iv[0][1])
    iv[-1] = (iv[-1][0], t1)
    merged = [list(iv[0])]
    for a, b in iv[1:]:
        if a - merged[-1][1] <= args.max_pause:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


# ─────────────────────────────────────────────────────────────
# 5) 얼굴 따라가기 (crop 레이아웃)
# ─────────────────────────────────────────────────────────────
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")


def face_detector(sw: int, sh: int):
    """YuNet(정확, auto_editor 와 같은 모델 파일 공유) → 없으면 Haar → 없으면 None."""
    import cv2
    model = Path(__file__).with_name("face_detection_yunet.onnx")
    try:
        if not model.exists():
            log("   얼굴 인식 모델 내려받는 중…")
            urllib.request.urlretrieve(YUNET_URL, model)
        yn = cv2.FaceDetectorYN.create(str(model), "", (sw, sh), 0.6)
        return lambda img: [] if (r := yn.detect(img)[1]) is None else [tuple(f[:4]) for f in r]
    except Exception as e:
        log(f"   [안내] YuNet 사용 불가({e}) → 기본 얼굴 인식 사용")
    xml = Path(getattr(getattr(cv2, "data", None), "haarcascades", "")) / "haarcascade_frontalface_default.xml"
    if xml.exists():
        cas = cv2.CascadeClassifier(str(xml))
        if not cas.empty():
            return lambda img: list(cas.detectMultiScale(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 1.1, 5,
                                                         minSize=(sw // 14, sw // 14)))
    log("   [경고] 얼굴 인식 모델이 없어 화면 가운데를 기준으로 자릅니다.")
    return None


def face_track(video: Path, info: dict, t0: float, t1: float) -> tuple[np.ndarray, np.ndarray, float]:
    """(시간, 얼굴 중심 x 비율, 얼굴이 보인 비율). 초당 3번 검사."""
    sw = 480
    sh = int(round(sw * info["h"] / info["w"] / 2)) * 2
    detect = face_detector(sw, sh)
    if detect is None:
        return np.array([t0, t1]), np.array([0.5, 0.5]), 0.0
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}",
                          "-i", str(video), "-an", "-vf", f"fps=3,scale={sw}:{sh}",
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True).stdout
    fs = sw * sh * 3
    n = len(raw) // fs
    ts, xs, hit = [], [], 0
    last = 0.5
    for k in range(n):
        img = np.frombuffer(raw[k * fs:(k + 1) * fs], np.uint8).reshape(sh, sw, 3)
        f = detect(img)
        if len(f):
            x, y, w, h = max(f, key=lambda r: r[2] * r[3])
            last = (x + w / 2) / sw
            hit += 1
        ts.append(t0 + k / 3)
        xs.append(last)
    if not ts:
        return np.array([t0, t1]), np.array([0.5, 0.5]), 0.0
    xs = np.array(xs)
    # 흔들리지 않게: 이동 평균 + 작은 움직임 무시
    if len(xs) > 3:
        k = min(7, len(xs))
        xs = np.convolve(np.pad(xs, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, "valid")
        held = [xs[0]]
        for v in xs[1:]:
            held.append(v if abs(v - held[-1]) > 0.04 else held[-1])
        xs = np.array(held)
        xs = np.convolve(np.pad(xs, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, "valid")
    return np.array(ts), xs, hit / max(n, 1)


# ─────────────────────────────────────────────────────────────
# 6) 자막·제목 그리기
# ─────────────────────────────────────────────────────────────
class TextPainter:
    def __init__(self, font_path: str | None):
        from PIL import ImageFont
        self.ok = font_path is not None
        self.font_path = font_path
        self._fonts = {}
        self.ImageFont = ImageFont

    def font(self, size: int):
        if size not in self._fonts:
            self._fonts[size] = self.ImageFont.truetype(self.font_path, size)
        return self._fonts[size]

    @staticmethod
    def _wrap(draw, text: str, font, max_w: int, stroke: int) -> list[str]:
        lines, cur = [], ""
        for tok in re.split(r"(\s+)", text.strip()):
            if not tok:
                continue
            trial = cur + tok
            if cur and draw.textlength(trial.strip(), font=font) + stroke * 2 > max_w:
                lines.append(cur.strip())
                cur = tok.lstrip()
            else:
                cur = trial
        if cur.strip():
            lines.append(cur.strip())
        # 띄어쓰기 없는 긴 줄은 글자 단위로 자르기
        out = []
        for ln in lines:
            while draw.textlength(ln, font=font) + stroke * 2 > max_w and len(ln) > 1:
                k = len(ln)
                while k > 1 and draw.textlength(ln[:k], font=font) + stroke * 2 > max_w:
                    k -= 1
                out.append(ln[:k])
                ln = ln[k:]
            out.append(ln)
        return out

    def render(self, text: str, size: int, max_w: int, colors: list, stroke: int = 0,
               box: tuple | None = None, max_lines: int = 3):
        """BGR(float32), alpha(float32, HxWx1) 반환."""
        from PIL import Image, ImageDraw
        f = self.font(size)
        tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        lines = self._wrap(tmp, text, f, max_w, stroke)[:max_lines]
        asc, desc = f.getmetrics()
        lh = asc + desc
        gap = int(size * 0.18)
        widths = [int(tmp.textlength(ln, font=f)) for ln in lines]
        padx, pady = (int(size * 0.45), int(size * 0.3)) if box else (stroke, stroke)
        W = max(widths) + padx * 2 + stroke * 2
        H = lh * len(lines) + gap * (len(lines) - 1) + pady * 2
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if box:
            d.rounded_rectangle([0, 0, W - 1, H - 1], radius=int(size * 0.35), fill=box)
        y = pady
        for n, (ln, w) in enumerate(zip(lines, widths)):
            c = colors[min(n, len(colors) - 1)]
            d.text(((W - w) // 2, y), ln, font=f, fill=c,
                   stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            y += lh + gap
        a = np.asarray(img).astype(np.float32)
        return a[..., 2::-1].copy(), (a[..., 3:4] / 255.0)


def blend(frame: np.ndarray, layer, cx: int, top: int) -> None:
    bgr, al = layer
    h, w = al.shape[:2]
    x0 = max(0, cx - w // 2)
    y0 = max(0, top)
    x1, y1 = min(OW, x0 + w), min(OH, y0 + h)
    if x1 <= x0 or y1 <= y0:
        return
    bgr, al = bgr[: y1 - y0, : x1 - x0], al[: y1 - y0, : x1 - x0]
    roi = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = (roi * (1 - al) + bgr * al).astype(np.uint8)


def caption_chunks(words: list, t0: float, t1: float, max_chars: int = 14,
                   max_dur: float = 2.4) -> list[dict]:
    ws = [w for w in words if w["start"] >= t0 - 0.05 and w["end"] <= t1 + 0.05]
    chunks, cur = [], []

    def flush():
        if cur:
            text = "".join(w["word"] for w in cur).strip()
            if text:
                chunks.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": text})
        cur.clear()

    for i, w in enumerate(ws):
        tok = w["word"].strip()
        if cur:
            length = len("".join(x["word"] for x in cur).strip()) + len(tok)
            gap = w["start"] - cur[-1]["end"]
            if length > max_chars or w["end"] - cur[0]["start"] > max_dur or gap > 0.6:
                flush()
        cur.append(w)
        if END_PUNCT.search(tok):
            flush()
    flush()
    # 자막 사이 짧은 빈틈은 메워서 깜빡이지 않게
    for a, b in zip(chunks, chunks[1:]):
        if b["start"] - a["end"] < 0.35:
            a["end"] = b["start"]
    return chunks


# ─────────────────────────────────────────────────────────────
# 7) 렌더링
# ─────────────────────────────────────────────────────────────
def write_wav(path: Path, a: np.ndarray) -> None:
    import wave
    pcm = (np.clip(a, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def load_audio(video: Path, has_audio: bool, t0: float, t1: float) -> np.ndarray:
    n = int(round((t1 - t0) * SR))
    if not has_audio:
        return np.zeros((n, 2), np.float32)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}",
                          "-i", str(video), "-vn", "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, np.float32).reshape(-1, 2)
    if len(a) < n:
        a = np.vstack([a, np.zeros((n - len(a), 2), np.float32)])
    return a[:n]


def choose_layout(info: dict, args, track) -> str:
    if info["w"] / info["h"] <= OW / OH + 0.02:
        return "portrait"                      # 이미 세로 영상
    if args.layout != "auto":
        return args.layout
    return "crop" if track[2] >= 0.6 else "blur"


def render_short(video: Path, info: dict, words: list, s: dict, out_path: Path,
                 painter: TextPainter, args) -> dict:
    t0, t1 = float(s["start"]), float(s["end"])
    fps = info["fps"]
    track = None
    landscape = info["w"] / info["h"] > OW / OH + 0.02
    if landscape and args.layout in ("auto", "crop"):
        track = face_track(video, info, t0, t1)
    layout = choose_layout(info, args, track or (None, None, 0.0))

    # 디코딩 필터
    if layout == "crop":
        sw = int(round(info["w"] * OH / info["h"] / 2)) * 2
        vf = f"fps={fps},scale={sw}:{OH}"
        fw, fh = sw, OH
    elif layout == "portrait":
        vf = (f"fps={fps},split[a][b];"
              f"[a]scale={OW}:{OH}:force_original_aspect_ratio=increase,crop={OW}:{OH},"
              f"boxblur=24:2,eq=brightness=-0.10[bg];"
              f"[b]scale={OW}:{OH}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
        fw, fh = OW, OH
    else:  # blur
        fgw = int(OW * args.zoom) // 2 * 2
        vf = (f"fps={fps},split[a][b];"
              f"[a]scale={OW}:{OH}:force_original_aspect_ratio=increase,crop={OW}:{OH},"
              f"boxblur=24:2,eq=brightness=-0.12[bg];"
              f"[b]scale={fgw}:-2[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
        fw, fh = OW, OH

    # 남길 프레임
    keep_iv = keep_intervals(words, t0, t1, args)
    n_frames = int(round((t1 - t0) * fps))
    ft = t0 + (np.arange(n_frames) + 0.5) / fps
    keep = np.zeros(n_frames, bool)
    for a, b in keep_iv:
        keep |= (ft >= a) & (ft < b)
    if not keep.any():
        keep[:] = True
    cum = np.concatenate([[0], np.cumsum(keep)])          # 원본 프레임 → 출력 프레임

    def out_t(t: float) -> float:
        i = int(np.clip(round((t - t0) * fps), 0, n_frames))
        return cum[i] / fps

    # 오디오: 남긴 프레임 구간만 이어 붙이기 (이음새는 짧게 페이드)
    src = load_audio(video, info["audio"], t0, t1)
    parts, i = [], 0
    while i < n_frames:
        if keep[i]:
            j = i
            while j < n_frames and keep[j]:
                j += 1
            a = src[int(i * SR / fps): int(j * SR / fps)].copy()
            fl = min(int(0.008 * SR), len(a) // 2)
            if fl > 0:
                ramp = np.linspace(0, 1, fl, dtype=np.float32)[:, None]
                a[:fl] *= ramp
                a[-fl:] *= ramp[::-1]
            parts.append(a)
            i = j
        else:
            i += 1
    audio_out = np.vstack(parts) if parts else np.zeros((1, 2), np.float32)

    # 자막·제목 미리 그리기
    caps, title_layer = [], None
    if painter.ok and not args.no_captions:
        for c in caption_chunks(words, t0, t1):
            a, b = out_t(c["start"]), out_t(c["end"] + 0.05)
            if b - a > 0.05:
                caps.append((a, b, painter.render(c["text"], 74, 920, [(255, 255, 255, 255)],
                                                  stroke=7, max_lines=2)))
    if painter.ok and not args.no_title and s.get("title"):
        title_layer = painter.render(s["title"], 80, 940, [(255, 255, 255, 255), (255, 225, 77, 255)],
                                     box=(0, 0, 0, 175), max_lines=2)

    # 레이아웃별 글자 위치
    if layout == "blur":
        fg_h = int(int(OW * args.zoom) // 2 * 2 * info["h"] / info["w"])
        cap_top = (OH + fg_h) // 2 + 40
        title_top = max(200, (OH - fg_h) // 2 - 40 - (title_layer[1].shape[0] if title_layer else 0))
    else:
        cap_top, title_top = 1260, 230
    cap_top = min(cap_top, 1420)            # 아래쪽 쇼츠 UI(좋아요·설명)에 가리지 않게

    W2 = OW
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}",
                            "-i", str(video), "-an", "-filter_complex", vf,
                            "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td, "audio.wav")
        write_wav(wav, audio_out)
        enc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OW}x{OH}", "-r", f"{fps}", "-i", "-",
             "-i", str(wav), "-map", "0:v", "-map", "1:a",
             "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
             "-movflags", "+faststart", "-shortest", str(out_path.resolve())],
            stdin=subprocess.PIPE)
        fsize = fw * fh * 3
        i = k = 0
        last_frame = None
        while i < n_frames:
            buf = dec.stdout.read(fsize)
            if len(buf) < fsize:
                break
            if keep[i]:
                fr = np.frombuffer(buf, np.uint8).reshape(fh, fw, 3)
                if layout == "crop":
                    cx = float(np.interp(t0 + i / fps, track[0], track[1])) * fw
                    x0 = int(np.clip(cx - W2 / 2, 0, fw - W2))
                    frame = fr[:, x0:x0 + W2].copy()
                else:
                    frame = fr.copy()
                t = k / fps
                if title_layer:
                    blend(frame, title_layer, OW // 2, title_top)
                for a, b, layer in caps:
                    if a <= t < b:
                        blend(frame, layer, OW // 2, cap_top)
                        break
                enc.stdin.write(frame.tobytes())
                last_frame = frame
                k += 1
            i += 1
        # 디코딩이 몇 프레임 모자라면 마지막 장면으로 채움
        need_frames = int(round(len(audio_out) / SR * fps))
        while last_frame is not None and k < need_frames:
            enc.stdin.write(last_frame.tobytes())
            k += 1
        dec.kill()               # 필요한 프레임을 다 읽었으면 디코더는 바로 끝냄
        dec.wait()
        dec.stdout.close()
        enc.stdin.close()
        if enc.wait() != 0:
            raise RuntimeError("ffmpeg 인코딩 실패")
    return {"layout": layout, "length": round(k / fps, 1), "cut": round((t1 - t0) - k / fps, 1)}


# ─────────────────────────────────────────────────────────────
# 메인 처리
# ─────────────────────────────────────────────────────────────
def process(video: Path, args) -> Path:
    video = video.resolve()
    info = probe(video)
    out_dir = args.out or video.with_name(video.stem + "_shorts")
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"\n▶ {video.name}  ({info['w']}x{info['h']}, {info['fps']:.0f}fps, {fmt_ts(info['duration'])})")
    if info["duration"] < args.min_len:
        sys.exit(f"[오류] 영상이 {args.min_len:.0f}초보다 짧아 쇼츠로 자를 구간이 없습니다.")

    tr_path = out_dir / "transcript.json"
    if tr_path.exists():
        log("▶ 저장된 대본을 사용합니다 (transcript.json)")
        tr = json.loads(tr_path.read_text(encoding="utf-8"))
    else:
        tr = transcribe(video, args.whisper_model, args.language)
        tr_path.write_text(json.dumps(tr, ensure_ascii=False, indent=1), encoding="utf-8")
    words = tr["words"]
    if not words:
        sys.exit("[오류] 영상에서 말소리를 찾지 못했습니다. 말이 있는 영상인지 확인하세요.")
    sents = build_sentences(words)

    plan_path = out_dir / "shorts_plan.json"
    if plan_path.exists() and not args.replan:
        log("▶ 저장된 계획을 사용합니다 (shorts_plan.json, 새로 고르려면 --replan)")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    else:
        plan = make_plan(video, info, sents, args)
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if not plan["shorts"]:
        sys.exit("[오류] 쇼츠로 만들 만한 구간을 찾지 못했습니다. --min-len 을 줄여 보세요.")

    font = find_font()
    if not font:
        log("   [안내] 한글 글꼴을 찾지 못해 자막과 제목 없이 만듭니다.")
    painter = TextPainter(font)

    results = []
    for s in plan["shorts"]:
        if args.only and s["id"] not in args.only:
            continue
        out = out_dir / f"short_{s['id']:02d}.mp4"
        log(f"▶ 쇼츠 {s['id']}: {fmt_ts(s['start'])}~{fmt_ts(s['end'])}  「{s.get('title', '')}」")
        r = render_short(video, info, words, s, out, painter, args)
        log(f"   완료 → {out.name}  ({r['length']}초, 레이아웃 {r['layout']}, 쉼 {r['cut']}초 제거)")
        results.append((s, r))

    write_report(out_dir, video, plan, results)
    log(f"\n✔ 끝났습니다 → {out_dir}")
    return out_dir


LAYOUT_KO = {"blur": "흐린 배경", "crop": "얼굴 따라 자르기", "portrait": "원본 세로"}


def write_report(out_dir: Path, video: Path, plan: dict, results: list) -> None:
    done = {s["id"]: r for s, r in results}
    L = [f"# 쇼츠 정리 — {video.name}", "",
         f"구간 선택: {'Claude' if plan['planner'] == 'claude' else '규칙 기반'}  ",
         "업로드할 때 아래 제목·설명·해시태그를 복사해 쓰세요. 시간이나 제목을 바꾸려면 "
         "`shorts_plan.json` 을 고치고 다시 실행하면 됩니다.", ""]
    for s in plan["shorts"]:
        r = done.get(s["id"])
        L += [f"## {s['id']}. {s.get('upload_title') or s.get('title', '')}", "",
              f"- 파일: `short_{s['id']:02d}.mp4`"
              + (f" ({r['length']}초, {LAYOUT_KO.get(r['layout'], r['layout'])})" if r else " (이번에 렌더링 안 함)"),
              f"- 원본 구간: {fmt_ts(s['start'])} ~ {fmt_ts(s['end'])}",
              f"- 화면 제목: {s.get('title', '')}",
              f"- 고른 이유: {s.get('reason', '')} (점수 {s.get('score', '')})",
              f"- 첫 문장: {s.get('first_line', '')}", "",
              "**업로드 제목**", "```", f"{s.get('upload_title', '')} #shorts", "```",
              "**설명**", "```", s.get("description", ""), "", " ".join(s.get("hashtags", [])), "```", ""]
    (out_dir / "shorts_report.md").write_text("\n".join(L), encoding="utf-8")


def watch(folder: Path, args) -> None:
    log(f"▶ '{folder}' 폴더를 지켜보는 중… 영상을 넣으면 쇼츠를 자동으로 만듭니다. (끝내려면 Ctrl+C)")
    sizes: dict[Path, int] = {}
    while True:
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() not in VIDEO_EXT or not f.is_file():
                continue
            if (f.with_name(f.stem + "_shorts") / "shorts_report.md").exists():
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
                f.with_name(f.stem + "_shorts").mkdir(exist_ok=True)
                (f.with_name(f.stem + "_shorts") / "shorts_report.md").write_text(str(e), encoding="utf-8")
            except Exception as e:
                log(f"[오류] {f.name}: {e}")
        time.sleep(10)


def main() -> None:
    ap = argparse.ArgumentParser(description="긴 영상에서 핵심 구간을 골라 세로 쇼츠로 자동 생성")
    ap.add_argument("video", type=Path, nargs="?", help="원본 영상 파일")
    ap.add_argument("--watch", type=Path, help="이 폴더에 들어오는 영상을 자동 처리")
    ap.add_argument("--out", type=Path, help="출력 폴더 (기본: <영상이름>_shorts)")
    # 구간
    ap.add_argument("--count", type=int, default=3, help="만들 쇼츠 개수 (기본 3)")
    ap.add_argument("--min-len", type=float, default=20, help="쇼츠 최소 길이(초, 기본 20)")
    ap.add_argument("--max-len", type=float, default=58, help="쇼츠 최대 길이(초, 기본 58)")
    ap.add_argument("--only", type=int, nargs="+", help="이 번호의 쇼츠만 렌더링 (예: --only 1 3)")
    # 화면
    ap.add_argument("--layout", choices=["auto", "blur", "crop"], default="auto",
                    help="auto: 얼굴 많으면 crop / blur: 흐린 배경 + 원본 / crop: 얼굴 따라 자르기")
    ap.add_argument("--zoom", type=float, default=1.0, help="blur 레이아웃에서 원본 크기 (1.0~1.6)")
    ap.add_argument("--no-captions", action="store_true", help="자막 넣지 않기")
    ap.add_argument("--no-title", action="store_true", help="상단 제목 넣지 않기")
    ap.add_argument("--no-trim", action="store_true", help="구간 안의 쉼을 자르지 않기")
    ap.add_argument("--max-pause", type=float, default=0.5, help="이보다 긴 쉼은 잘라냄(초, 기본 0.5)")
    # 계획·인식
    ap.add_argument("--model", default=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"), help="Claude 모델 ID")
    ap.add_argument("--no-claude", action="store_true", help="Claude 를 쓰지 않음(규칙 기반)")
    ap.add_argument("--replan", action="store_true", help="저장된 shorts_plan.json 무시하고 다시 고르기")
    ap.add_argument("--whisper-model", default="small", help="tiny/base/small/medium/large-v3")
    ap.add_argument("--language", default="ko")
    ap.add_argument("--crf", type=int, default=20, help="화질(낮을수록 고화질, 18~23)")
    args = ap.parse_args()
    args.zoom = min(max(args.zoom, 1.0), 1.6)
    if args.min_len >= args.max_len:
        sys.exit("[오류] --min-len 은 --max-len 보다 작아야 합니다.")

    need("ffmpeg"); need("ffprobe")
    if args.watch:
        args.out = None
        args.watch.mkdir(exist_ok=True)
        watch(args.watch, args)
    elif args.video:
        if not args.video.exists():
            sys.exit(f"[오류] 파일이 없습니다: {args.video}")
        process(args.video, args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
