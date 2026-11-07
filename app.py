#!/usr/bin/env python3
"""
app.py — 영상 자동화 웹앱 (내 컴퓨터에서만 열리는 로컬 웹앱)

auto_editor.py(영상 편집)와 auto_shorts.py(쇼츠 만들기)를 버튼으로 쓸 수 있게 해 줍니다.
추가 설치 없이 파이썬 기본 기능만 씁니다.

  python app.py              → 브라우저가 열리고 http://127.0.0.1:8765 에서 동작
  python app.py --port 9000  → 다른 포트로 실행
  python app.py --no-browser → 브라우저 자동으로 열지 않기
"""
from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import mimetypes
import os
import platform
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
UPLOAD_DIR = BASE / "업로드"
HTML_FILE = BASE / "webapp.html"
# 키는 OneDrive·바탕화면 밖(사용자 AppData)에 저장: 동기화되지 않고, 윈도우 '랜섬웨어 보호'에도 막히지 않음
SETTINGS_DIR = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "VideoAutomationStudio"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
LEGACY_SETTINGS = BASE / "webapp_settings.json"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
SCAN_DIRS = [BASE, BASE / "업로드", BASE / "쇼츠"]
KEY_NAMES = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"]
IS_WIN = platform.system() == "Windows"


# ─────────────────────────────────────────────────────────────
# 설정 (API 키)
# ─────────────────────────────────────────────────────────────
def load_settings() -> dict:
    for f in (SETTINGS_FILE, LEGACY_SETTINGS):
        try:
            return json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
    return {}


def save_settings(s: dict) -> None:
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"키를 저장할 파일을 만들지 못했습니다 ({SETTINGS_FILE}): {e}")


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    for k, v in load_settings().get("keys", {}).items():
        if k in KEY_NAMES and v:
            env[k] = v
    return env


def current_key(k: str) -> str:
    return (load_settings().get("keys", {}).get(k) or os.environ.get(k, "")).strip()


def key_warning(k: str, v: str) -> str:
    """키 모양만 보고 흔한 실수를 알려 줌."""
    if not v:
        return ""
    # Claude 콘솔 키는 sk-ant-api03-(예전 방식), sk-ant-usr-(개인 키), sk-ant-svc-(서비스 계정) 등 여러 형식이 있음
    if k == "ANTHROPIC_API_KEY":
        if v.startswith("sk-ant-oat"):
            return "Claude 구독용 토큰이라 API에 쓸 수 없습니다. Claude 콘솔에서 만든 API 키를 넣어 주세요"
        if v.startswith("sk-ant-admin"):
            return "관리자 키라 Claude를 호출할 수 없습니다. 일반 API 키를 넣어 주세요"
        if not v.startswith("sk-ant-"):
            return "Claude API 키는 sk-ant- 로 시작해야 합니다"
    if k == "OPENAI_API_KEY" and not v.startswith("sk-"):
        return "OpenAI API 키는 sk- 로 시작해야 합니다"
    if re.search(r"\s", v):
        return "키 중간에 띄어쓰기나 줄바꿈이 들어 있습니다"
    return ""


def key_status() -> dict:
    saved = load_settings().get("keys", {})
    out = {}
    for k in KEY_NAMES:
        v = current_key(k)
        out[k] = {"set": bool(v), "source": "웹앱" if saved.get(k) else ("시스템" if v else ""),
                  "hint": (v[:10] + "…" + v[-4:]) if len(v) > 16 else "",
                  "warn": key_warning(k, v)}
    return out


def test_keys() -> list[dict]:
    """등록된 키로 각 서비스에 '모델 목록'만 물어봐서 키가 살아 있는지 확인 (비용 없음)."""
    checks = [
        ("Claude", "ANTHROPIC_API_KEY", lambda v: urllib.request.Request(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": v, "anthropic-version": "2023-06-01"})),
        ("OpenAI", "OPENAI_API_KEY", lambda v: urllib.request.Request(
            "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {v}"})),
        ("Gemini", "GEMINI_API_KEY", lambda v: urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models?key=" + urllib.parse.quote(v))),
    ]
    out = []
    for name, k, make in checks:
        v = current_key(k)
        if not v:
            continue
        try:
            with urllib.request.urlopen(make(v), timeout=15):
                pass
            out.append({"name": name, "ok": True, "msg": "정상 연결됨"})
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")
            msg = {401: "키가 올바르지 않습니다 (잘못 복사했거나, 삭제된 키)",
                   403: "이 키로는 접근 권한이 없습니다", 429: "사용량 한도 초과 또는 결제 정보 필요"}
            m = msg.get(e.code, f"오류 {e.code}")
            if "workspace" in detail.lower():
                m = ("키가 여러 워크스페이스에 걸쳐 있어 쓸 수 없습니다. 콘솔에서 키를 새로 만들 때 "
                     "워크스페이스를 하나(예: Default)만 골라 주세요")
            elif "credit" in detail.lower():
                m = "크레딧이 부족합니다. 콘솔 Billing 에서 충전해 주세요"
            out.append({"name": name, "ok": False, "msg": m})
        except Exception as e:
            out.append({"name": name, "ok": False, "msg": f"인터넷 연결 확인 필요 ({e})"})
        if out[-1]["ok"] is False and key_warning(k, v):
            out[-1]["msg"] += " — " + key_warning(k, v)
    return out


def deps_status() -> dict:
    mods = {"faster_whisper": "faster-whisper", "cv2": "opencv-python", "numpy": "numpy",
            "PIL": "pillow", "easyocr": "easyocr"}
    missing = [pkg for mod, pkg in mods.items() if importlib.util.find_spec(mod) is None]
    return {"ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
            "missing": missing,
            "editor": (BASE / "auto_editor.py").exists(),
            "shorts": (BASE / "auto_shorts.py").exists()}


# ─────────────────────────────────────────────────────────────
# 영상·결과 찾기
# ─────────────────────────────────────────────────────────────
def rel(p: Path) -> str:
    return p.resolve().relative_to(BASE).as_posix()


def safe_path(r: str) -> Path:
    p = (BASE / urllib.parse.unquote(r)).resolve()
    if p != BASE and BASE not in p.parents:
        raise PermissionError("허용되지 않은 경로")
    return p


def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def shorts_info(d: Path, source: Path) -> dict | None:
    if not d.is_dir():
        return None
    plan = read_json(d / "shorts_plan.json") or {"shorts": []}
    items = []
    for s in plan.get("shorts", []):
        f = d / f"short_{int(s['id']):02d}.mp4"
        items.append({**s, "file": rel(f) if f.exists() else None,
                      "mtime": f.stat().st_mtime if f.exists() else 0})
    return {"dir": rel(d), "source": rel(source), "planner": plan.get("planner"),
            "plan": rel(d / "shorts_plan.json") if (d / "shorts_plan.json").exists() else None,
            "report": rel(d / "shorts_report.md") if (d / "shorts_report.md").exists() else None,
            "items": items}


def list_videos() -> list[dict]:
    vids = []
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir(), key=lambda x: -x.stat().st_mtime):
            if not f.is_file() or f.suffix.lower() not in VIDEO_EXT:
                continue
            st = f.stat()
            ed_dir = f.with_name(f.stem + "_edited")
            ed_mp4 = ed_dir / f"{f.stem}_edited.mp4"
            edited = None
            if ed_mp4.exists():
                edited = {"file": rel(ed_mp4), "dir": rel(ed_dir), "mtime": ed_mp4.stat().st_mtime,
                          "report": rel(ed_dir / "report.md") if (ed_dir / "report.md").exists() else None}
            shorts = [x for x in (shorts_info(f.with_name(f.stem + "_shorts"), f),
                                  shorts_info(ed_dir / f"{f.stem}_edited_shorts", ed_mp4)) if x]
            vids.append({"path": rel(f), "name": f.name, "folder": rel(d) if d != BASE else "",
                         "size": st.st_size, "mtime": st.st_mtime,
                         "edited": edited, "shorts": shorts})
    return vids


# ─────────────────────────────────────────────────────────────
# 작업 대기열 (무거운 작업이라 한 번에 하나씩 실행)
# ─────────────────────────────────────────────────────────────
class Job:
    _ids = itertools.count(1)

    def __init__(self, title: str, steps: list[list[str]], video: str):
        self.id = next(self._ids)
        self.title, self.steps, self.video = title, steps, video
        self.status = "queued"           # queued / running / done / failed / canceled
        self.log: list[str] = []
        self.created, self.started, self.ended = time.time(), None, None
        self.proc: subprocess.Popen | None = None
        self.cancel = False

    def brief(self) -> dict:
        last = next((l for l in reversed(self.log) if l.strip()), "")
        return {"id": self.id, "title": self.title, "video": self.video, "status": self.status,
                "created": self.created, "started": self.started, "ended": self.ended,
                "last": last[-140:], "lines": len(self.log)}


JOBS: dict[int, Job] = {}
JOB_Q: "queue.Queue[Job]" = queue.Queue()


def kill_tree(p: subprocess.Popen) -> None:
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGTERM)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def run_job(job: Job) -> None:
    job.status, job.started = "running", time.time()
    for n, cmd in enumerate(job.steps, 1):
        if job.cancel:
            break
        if len(job.steps) > 1:
            job.log.append(f"━━━ {n}/{len(job.steps)} 단계 ━━━")
        job.log.append("$ " + " ".join(Path(c).name if i < 2 else c for i, c in enumerate(cmd)))
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WIN else {"start_new_session": True}
        try:
            job.proc = subprocess.Popen(cmd, cwd=BASE, env=child_env(), stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)
        except Exception as e:
            job.log.append(f"[오류] 실행하지 못했습니다: {e}")
            job.status = "failed"
            break
        for line in job.proc.stdout:
            job.log.append(line.rstrip("\n"))
            if len(job.log) > 5000:
                del job.log[:1000]
        code = job.proc.wait()
        if job.cancel:
            break
        if code != 0:
            job.status = "failed"
            job.log.append(f"[실패] 종료 코드 {code}")
            break
    if job.cancel:
        job.status = "canceled"
        job.log.append("[취소됨]")
    elif job.status == "running":
        job.status = "done"
        job.log.append("✔ 완료")
    job.ended = time.time()
    job.proc = None


def worker() -> None:
    while True:
        job = JOB_Q.get()
        if job.cancel:
            job.status, job.ended = "canceled", time.time()
            continue
        try:
            run_job(job)
        except Exception as e:
            job.status, job.ended = "failed", time.time()
            job.log.append(f"[오류] {e}")


def opt_flags(opts: dict, spec: dict) -> list[str]:
    """화면 옵션 → 명령줄 옵션. spec: {옵션이름: ('flag'|'value', 허용값 검사 함수)}"""
    out = []
    for name, (kind, check) in spec.items():
        v = opts.get(name)
        if v in (None, "", False):
            continue
        if kind == "flag":
            out.append(f"--{name}")
        elif check(v):
            out += [f"--{name}", str(v)]
    return out


def num(lo, hi):
    def f(v):
        try:
            return lo <= float(v) <= hi
        except Exception:
            return False
    return f


EDIT_SPEC = {"blur-faces": ("flag", None), "no-illustrations": ("flag", None),
             "illustration-mode": ("value", lambda v: v in ("full", "overlay")),
             "max-pause": ("value", num(0.2, 3)), "replan": ("flag", None),
             "whisper-model": ("value", lambda v: v in ("tiny", "base", "small", "medium", "large-v3"))}
SHORTS_SPEC = {"count": ("value", num(1, 20)), "min-len": ("value", num(5, 170)),
               "max-len": ("value", num(10, 180)),
               "layout": ("value", lambda v: v in ("auto", "blur", "crop")),
               "zoom": ("value", num(1, 1.6)), "no-title": ("flag", None),
               "no-captions": ("flag", None), "no-trim": ("flag", None),
               "replan": ("flag", None),
               "whisper-model": ("value", lambda v: v in ("tiny", "base", "small", "medium", "large-v3"))}


def make_job(body: dict) -> Job:
    action = body.get("action")
    py = sys.executable
    if action == "install":
        return Job("필요한 프로그램 설치", [[py, "-m", "pip", "install", "-r", "requirements.txt"]], "")
    video = safe_path(body["video"])
    if not video.is_file():
        raise FileNotFoundError("영상 파일이 없습니다")
    ed_flags = opt_flags(body.get("edit", {}), EDIT_SPEC)
    sh_flags = opt_flags(body.get("shorts", {}), SHORTS_SPEC)
    edited = video.with_name(video.stem + "_edited") / f"{video.stem}_edited.mp4"
    editor, shorts = str(BASE / "auto_editor.py"), str(BASE / "auto_shorts.py")
    if action == "edit":
        return Job(f"영상 편집 · {video.name}", [[py, editor, str(video)] + ed_flags], rel(video))
    if action == "shorts":
        src = edited if body.get("from_edited") and edited.exists() else video
        only = [str(int(x)) for x in body.get("only", [])]
        flags = sh_flags + (["--only"] + only if only else [])
        label = "쇼츠 다시 만들기" if only else "쇼츠 만들기"
        return Job(f"{label} · {src.name}", [[py, shorts, str(src)] + flags], rel(video))
    if action == "both":
        return Job(f"편집 → 쇼츠 · {video.name}",
                   [[py, editor, str(video)] + ed_flags, [py, shorts, str(edited)] + sh_flags], rel(video))
    raise ValueError("알 수 없는 작업")


# ─────────────────────────────────────────────────────────────
# 쇼츠 계획 고치기
# ─────────────────────────────────────────────────────────────
def save_plan_item(plan_rel: str, item: dict) -> None:
    p = safe_path(plan_rel)
    if p.name != "shorts_plan.json":
        raise PermissionError("계획 파일이 아닙니다")
    plan = json.loads(p.read_text(encoding="utf-8"))
    for s in plan["shorts"]:
        if int(s["id"]) == int(item["id"]):
            for k in ("title", "upload_title", "description"):
                if k in item:
                    s[k] = str(item[k])
            if "hashtags" in item:
                tags = item["hashtags"]
                if isinstance(tags, str):
                    tags = tags.split()
                s["hashtags"] = [t if t.startswith("#") else "#" + t for t in tags if t.strip("#")]
            if "start" in item and "end" in item:
                a, b = float(item["start"]), float(item["end"])
                if not (0 <= a < b):
                    raise ValueError("시작 시간은 끝 시간보다 앞이어야 합니다")
                s["start"], s["end"] = round(a, 2), round(b, 2)
            break
    else:
        raise KeyError("해당 쇼츠가 계획에 없습니다")
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def open_in_explorer(p: Path) -> None:
    target = p if p.is_dir() else p.parent
    if IS_WIN:
        if p.is_file():
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            os.startfile(str(target))  # type: ignore[attr-defined]
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


# ─────────────────────────────────────────────────────────────
# HTTP 서버
# ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "VideoAutomation/1.0"

    def log_message(self, fmt, *args):   # 터미널을 조용하게
        pass

    # ---- 응답 도우미
    def send_json(self, data, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, msg: str, code: int = 400) -> None:
        self.send_json({"error": msg}, code)

    def body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def same_origin(self) -> bool:
        """다른 웹사이트가 이 서버에 몰래 요청하지 못하게 막음."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urllib.parse.urlparse(origin).netloc == host

    # ---- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                body = HTML_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/state":
                self.send_json({"videos": list_videos(),
                                "jobs": [j.brief() for j in sorted(JOBS.values(), key=lambda j: -j.id)][:30],
                                "keys": key_status(), "deps": deps_status(),
                                "base": str(BASE)})
            elif re.fullmatch(r"/api/jobs/\d+", u.path):
                job = JOBS.get(int(u.path.rsplit("/", 1)[1]))
                if not job:
                    return self.fail("작업이 없습니다", 404)
                since = int(q.get("since", ["0"])[0])
                self.send_json({**job.brief(), "log": job.log[since:], "next": len(job.log)})
            elif u.path == "/api/text":
                p = safe_path(q["path"][0])
                if p.suffix.lower() not in (".md", ".json", ".txt"):
                    return self.fail("텍스트 파일만 볼 수 있습니다")
                self.send_json({"text": p.read_text(encoding="utf-8", errors="replace")})
            elif u.path == "/file":
                self.send_file(safe_path(q["path"][0]), "download" in q)
            else:
                self.fail("없는 주소입니다", 404)
        except (PermissionError, KeyError) as e:
            self.fail(str(e), 403)
        except FileNotFoundError:
            self.fail("파일이 없습니다", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_file(self, p: Path, download: bool) -> None:
        if not p.is_file():
            raise FileNotFoundError
        size = p.stat().st_size
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if download:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(p.name))
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # ---- POST
    def do_POST(self):
        if not self.same_origin():
            return self.fail("다른 사이트에서 온 요청은 받지 않습니다", 403)
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/upload":
                self.upload()
            elif u.path == "/api/jobs":
                job = make_job(self.body_json())
                JOBS[job.id] = job
                JOB_Q.put(job)
                self.send_json(job.brief())
            elif re.fullmatch(r"/api/jobs/\d+/cancel", u.path):
                job = JOBS.get(int(u.path.split("/")[3]))
                if not job:
                    return self.fail("작업이 없습니다", 404)
                job.cancel = True
                if job.proc:
                    kill_tree(job.proc)
                elif job.status == "queued":
                    job.status, job.ended = "canceled", time.time()
                self.send_json(job.brief())
            elif u.path == "/api/jobs/clear":
                for k in [k for k, j in JOBS.items() if j.status in ("done", "failed", "canceled")]:
                    del JOBS[k]
                self.send_json({"ok": True})
            elif u.path == "/api/plan":
                b = self.body_json()
                save_plan_item(b["plan"], b["item"])
                self.send_json({"ok": True})
            elif u.path == "/api/open":
                open_in_explorer(safe_path(self.body_json()["path"]))
                self.send_json({"ok": True})
            elif u.path == "/api/settings":
                b = self.body_json()
                s = load_settings()
                keys = s.setdefault("keys", {})
                for k, v in (b.get("keys") or {}).items():
                    if k not in KEY_NAMES:
                        continue
                    v = (v or "").strip()
                    if v == "__delete__":
                        keys.pop(k, None)
                    elif v:
                        keys[k] = v
                save_settings(s)
                self.send_json({"keys": key_status()})
            elif u.path == "/api/keytest":
                self.send_json({"results": test_keys()})
            else:
                self.fail("없는 주소입니다", 404)
        except (PermissionError,) as e:
            self.fail(str(e), 403)
        except FileNotFoundError as e:
            self.fail(str(e) or "파일이 없습니다", 404)
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            self.fail(f"요청을 처리하지 못했습니다: {e}")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:               # 어떤 오류든 화면에 이유를 보여 줌
            self.fail(str(e), 500)

    def upload(self) -> None:
        name = urllib.parse.unquote(self.headers.get("X-Filename", ""))
        name = re.sub(r'[\\/:*?"<>|]', "_", Path(name).name).strip()
        if not name or Path(name).suffix.lower() not in VIDEO_EXT:
            return self.fail("영상 파일(mp4, mov, mkv, avi, m4v, webm)만 올릴 수 있습니다")
        UPLOAD_DIR.mkdir(exist_ok=True)
        dest = UPLOAD_DIR / name
        stem, n = dest.stem, 2
        while dest.exists():
            dest = UPLOAD_DIR / f"{stem} ({n}){dest.suffix}"
            n += 1
        left = int(self.headers.get("Content-Length") or 0)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if left > 0:
                raise ConnectionError("업로드가 중간에 끊겼습니다")
            tmp.replace(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        self.send_json({"path": rel(dest), "name": dest.name})


def main() -> None:
    ap = argparse.ArgumentParser(description="영상 자동화 로컬 웹앱")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if not HTML_FILE.exists():
        sys.exit(f"[오류] {HTML_FILE.name} 이 app.py 와 같은 폴더에 있어야 합니다.")
    UPLOAD_DIR.mkdir(exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        sys.exit(f"[오류] {args.port}번 포트를 쓸 수 없습니다. 이미 웹앱이 켜져 있거나 다른 프로그램이 쓰고 있어요.\n"
                 f"       브라우저에서 http://127.0.0.1:{args.port} 을 열어 보거나 --port 9000 으로 실행하세요.")
    url = f"http://127.0.0.1:{args.port}"
    print(f"▶ 영상 자동화 웹앱이 켜졌습니다: {url}")
    print(f"  API 키 저장 위치: {SETTINGS_FILE}")
    print("  이 창을 닫으면 웹앱도 꺼집니다. (끝내려면 Ctrl+C)")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n웹앱을 끕니다.")
        for j in JOBS.values():
            if j.proc:
                kill_tree(j.proc)


if __name__ == "__main__":
    main()
