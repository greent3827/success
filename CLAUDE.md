# CLAUDE.md

이 저장소에서 작업하는 Claude를 위한 안내입니다. 사용자와는 **한국어**로 대화합니다.

## 프로젝트 한 줄 요약

전자책·정보성 유튜버의 영상 작업을 자동화하는 Windows용 도구 모음입니다. 긴 영상을 1차 편집하고(`auto_editor.py`), 핵심 구간을 세로 쇼츠로 만들고(`auto_shorts.py`), 이 둘을 브라우저 버튼으로 쓰게 해 주는 로컬 웹앱(`app.py` + `webapp.html`)이 있습니다.

사용자는 개발자가 아닙니다. 명령어보다 **bat 파일 더블클릭**과 **웹앱 버튼**으로 씁니다.

## 파일 구조

| 파일 | 역할 |
|---|---|
| `auto_editor.py` | 무음·필러 컷, 설명 일러스트 삽입, 개인정보 모자이크/삐 처리 → `<이름>_edited/` |
| `auto_shorts.py` | 핵심 구간 선택(Claude 또는 규칙), 9:16 변환(blur/crop/portrait), 자막·제목 → `<이름>_shorts/` |
| `app.py` | 로컬 웹 서버(파이썬 표준 라이브러리만 사용, 127.0.0.1:8765). 작업 대기열, 업로드, 파일 스트리밍, 키 관리 |
| `webapp.html` | 웹앱 화면(바닐라 JS, 외부 라이브러리 없음) |
| `처음설치.bat` | 파이썬·ffmpeg(winget)·패키지 설치 |
| `웹앱실행.bat` | 진짜 파이썬을 찾아 `app.py` 실행 (MS Store 가짜 `python` 바로가기 회피) |
| `영상편집.bat`, `폴더감시.bat`, `쇼츠만들기.bat`, `쇼츠폴더감시.bat` | 드래그앤드롭/폴더 감시 실행용 |
| `README_*.md` | 사용자용 설명서 (한국어) |

## 실행

```
python app.py                      # 웹앱 (브라우저 자동 열림)
python auto_editor.py 영상.mp4
python auto_shorts.py 영상.mp4 --count 3 --layout auto
```

`python app.py`는 감시 프로세스로 뜨고 실제 서버(`--serve`)를 자식으로 실행합니다. `app.py`가 바뀌면 진행 중인 작업이 끝난 뒤 서버를 다시 켜고, `webapp.html`이 바뀌면 브라우저가 스스로 새로 고칩니다(`/api/state`의 `ui_version`, `server_id`). `auto_*.py`는 작업마다 새로 실행되므로 바로 반영됩니다. 끄려면 `--no-reload`.

필요한 것: Python 3.10+, ffmpeg/ffprobe(PATH), `pip install -r requirements.txt`.

## 결과물과 재실행 규칙

- 결과 폴더에 `transcript.json`(음성 인식), `plan.json` / `shorts_plan.json`(계획)을 저장하고, 다시 실행하면 재사용합니다. 계획을 새로 짜려면 `--replan`.
- 사용자가 계획 JSON을 직접 고친 뒤 다시 실행하는 흐름을 지원해야 합니다. 계획 파일 형식을 바꿀 때는 예전 파일도 읽히게 하세요.
- 웹앱은 결과 폴더 이름 규칙(`<stem>_edited/<stem>_edited.mp4`, `<stem>_shorts/short_NN.mp4`, `<stem>_edited/<stem>_edited_shorts/`)에 의존합니다. 이름을 바꾸면 `app.py`의 `list_videos()`도 같이 고치세요.

## API 키

- 환경변수: `ANTHROPIC_API_KEY`(필수에 가까움), `OPENAI_API_KEY`(설명 그림). Gemini는 화면에서 뺐지만 코드에는 대안으로 남아 있습니다.
- 웹앱에서 넣은 키는 `%APPDATA%\VideoAutomationStudio\settings.json`에 저장합니다(OneDrive·바탕화면 밖). 예전 위치 `webapp_settings.json`도 읽기만 합니다.
- Claude 키 형식은 `sk-ant-api03-`, `sk-ant-usr-`(개인), `sk-ant-svc-` 등 여러 가지입니다. `sk-ant-oat`(구독 토큰), `sk-ant-admin`만 거부하세요.
- **키·비밀번호를 코드, 커밋, 로그, 답변에 절대 남기지 마세요.** `.env`와 설정 파일은 `.gitignore`에 있습니다.
- 키가 없으면 기능을 끄고 규칙 기반으로 계속 동작해야 합니다(실패로 멈추지 않기).

## 코드 규칙

- 사용자에게 보이는 모든 문구(로그, 오류, UI)는 **쉬운 한국어**로 씁니다. 오류에는 "무엇이 문제인지 + 어떻게 하면 되는지"를 함께 적습니다.
- `app.py`는 표준 라이브러리만 씁니다. 새 패키지가 꼭 필요하면 `requirements.txt`에 추가하고 README에 적으세요.
- Windows가 기본 대상입니다. 경로에 한글·공백이 있어도 동작해야 합니다(`subprocess`에는 리스트로 인자 전달).
- bat 파일은 **UTF-8 + CRLF**, 첫 줄 `chcp 65001`. `if (...)` 블록 안의 `echo`에 괄호를 쓰지 마세요(배치 문법이 깨짐).
- 영상 처리는 ffmpeg 디코딩 → numpy 프레임 처리 → ffmpeg 인코딩 파이프 구조입니다. 음량은 `loudnorm=I=-14`.
- 웹 서버는 127.0.0.1에만 바인딩하고, 파일 접근은 `safe_path()`로 저장소 폴더 안으로 제한하며, POST는 같은 출처만 받습니다. 이 보호를 약하게 만들지 마세요.

## 테스트

- 음성 인식 없이 시험하려면 결과 폴더에 가짜 `transcript.json`을 넣고 실행하면 됩니다(대본을 재사용함).
- 웹앱은 Playwright로 버튼을 눌러 확인합니다. 화면 변경 후에는 데스크톱 폭과 휴대폰 폭(390px) 둘 다 확인하세요.
- 사용자 PC에서 직접 확인하지 못한 부분은 "확인 못 함"이라고 분명히 알리세요.

## 올리면 안 되는 것

영상 파일(개인 영상 포함), `업로드/`, `쇼츠/`, `*_edited/`, `*_shorts/`, 모델 파일(`*.onnx`), 키·비밀번호 파일. 모두 `.gitignore`에 있습니다.
