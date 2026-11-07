# AGENTS.md

AI 코딩 에이전트(Codex, Cursor, Claude 등)를 위한 이 저장소 작업 규칙입니다. 자세한 설명은 [CLAUDE.md](CLAUDE.md)에 있습니다.

## 프로젝트

유튜버용 영상 자동화 도구 (Windows, Python).

- `auto_editor.py` — 긴 영상 1차 편집 (무음·필러 컷, 설명 그림, 개인정보 가리기)
- `auto_shorts.py` — 핵심 구간을 골라 세로 쇼츠 생성
- `app.py` + `webapp.html` — 위 두 기능을 버튼으로 쓰는 로컬 웹앱 (http://127.0.0.1:8765)

## 설치와 실행

```
pip install -r requirements.txt     # ffmpeg 는 별도 설치 (winget install Gyan.FFmpeg)
python app.py
python auto_shorts.py 영상.mp4 --count 3
```

## 반드시 지킬 것

1. **비밀 정보 금지**: API 키, 비밀번호, `.env`, 설정 파일을 커밋·로그·코드에 넣지 않습니다.
2. **영상 파일 금지**: `*.mp4` 등 영상과 결과 폴더(`*_edited/`, `*_shorts/`, `업로드/`, `쇼츠/`)는 커밋하지 않습니다.
3. **한국어 UI**: 사용자에게 보이는 문구는 모두 쉬운 한국어로 쓰고, 오류에는 해결 방법을 함께 적습니다.
4. **의존성 최소화**: `app.py`는 파이썬 표준 라이브러리만 씁니다.
5. **Windows 우선**: 한글·공백 경로를 지원하고, bat 파일은 UTF-8 + CRLF로 저장합니다.
6. **키 없이도 동작**: API 키가 없으면 해당 기능만 건너뛰고 나머지는 계속 동작해야 합니다.
7. **보안 유지**: 웹 서버의 127.0.0.1 바인딩, `safe_path()` 경로 제한, 같은 출처 검사를 약하게 만들지 않습니다.

## 변경 후 확인

- `python -m py_compile app.py auto_editor.py auto_shorts.py`
- 결과 폴더에 가짜 `transcript.json`을 넣어 음성 인식 없이 쇼츠 생성을 시험
- 웹앱 화면을 바꿨다면 데스크톱 폭과 휴대폰 폭에서 확인
