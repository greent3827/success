@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ==============================================
echo   GitHub 저장소에 올리기
echo ==============================================
echo.

rem ── 1. git 준비 ──────────────────────────────
where git >nul 2>nul
if %errorlevel%==0 goto havegit
echo git 이 없어서 설치합니다. 몇 분 걸릴 수 있어요.
winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
set "PATH=%PATH%;%ProgramFiles%\Git\cmd"
where git >nul 2>nul
if %errorlevel%==0 goto havegit
echo [오류] git 설치가 끝났다면 이 창을 닫고 이 파일을 다시 실행해 주세요.
pause
exit /b 1
:havegit

rem ── 2. 저장소 초기화 ─────────────────────────
if exist ".git" goto inited
echo [1/4] git 저장소를 만듭니다...
git init -b main
:inited
git config core.quotepath false
git config user.name >nul 2>nul
if not %errorlevel%==0 git config user.name "임영경"
git config user.email >nul 2>nul
if not %errorlevel%==0 git config user.email "fjqmdudrud@gmail.com"

rem ── 3. 올릴 파일 확인 ────────────────────────
echo.
echo [2/4] 올라갈 파일 목록입니다. 영상과 키 파일은 빠져 있어야 합니다.
git add -A
git status --short
git diff --cached --name-only | findstr /i /r "\.mp4$ \.mov$ \.mkv$ \.env$ settings\.json$" >nul
if %errorlevel%==0 goto danger
echo.
choice /c YN /m "이 파일들을 올릴까요"
if errorlevel 2 goto cancel

git diff --cached --quiet
if %errorlevel%==0 goto committed
git commit -m "영상 자동화 도구 업로드"
:committed

rem ── 4. GitHub 주소 연결 ──────────────────────
git remote get-url origin >nul 2>nul
if %errorlevel%==0 goto haveremote
echo.
echo [3/4] GitHub 저장소 주소를 입력하세요.
echo       예: https://github.com/내아이디/success  또는  내아이디/success
:askrepo
set "REPO="
set /p REPO=주소: 
if "%REPO%"=="" goto askrepo
echo %REPO% | findstr /b /i "http" >nul
if not %errorlevel%==0 set "REPO=https://github.com/%REPO%"
git remote add origin "%REPO%"
:haveremote

rem ── 5. 올리기 ────────────────────────────────
echo.
echo [4/4] GitHub 에 올립니다. 로그인 창이 뜨면 GitHub 계정으로 로그인하세요.
git push -u origin main
if %errorlevel%==0 goto done
echo.
echo GitHub 저장소에 이미 파일이 있어서 합친 뒤 다시 올립니다...
git pull origin main --allow-unrelated-histories --no-edit
git push -u origin main
if %errorlevel%==0 goto done
echo.
echo [오류] 올리지 못했습니다. 위 메시지를 복사해서 알려 주세요.
echo 주소가 틀렸다면 다음 명령으로 지운 뒤 다시 실행하세요:  git remote remove origin
pause
exit /b 1

:danger
echo.
echo [중지] 영상 파일이나 키 파일이 올라갈 뻔해서 멈췄습니다. 아무것도 올리지 않았어요.
git reset -q
pause
exit /b 1

:cancel
git reset -q
echo 취소했습니다. 아무것도 올리지 않았어요.
pause
exit /b 0

:done
echo.
echo ==============================================
echo   올리기 완료!
git remote get-url origin
echo ==============================================
pause
exit /b 0
