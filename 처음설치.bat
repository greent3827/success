@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ==============================================
echo   영상 자동화 스튜디오 - 처음 한 번 설치
echo ==============================================
echo.
echo [1/3] 파이썬 확인 중...
call :findpy
if defined PY goto havepy
echo 파이썬이 없어서 설치합니다. 몇 분 걸릴 수 있어요.
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
call :findpy
if defined PY goto havepy
echo.
echo [오류] 파이썬을 자동으로 설치하지 못했습니다.
echo https://www.python.org/downloads/ 에서 직접 설치하세요.
echo 설치 첫 화면 아래쪽의 Add python.exe to PATH 에 꼭 체크하세요.
pause
exit /b 1
:havepy
echo    찾은 파이썬: %PY%
echo.
echo [2/3] ffmpeg 확인 중...
where ffmpeg >nul 2>nul
if %errorlevel%==0 goto haveff
echo ffmpeg 를 설치합니다...
winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
:haveff
echo.
echo [3/3] 필요한 파이썬 패키지 설치 중... 처음에는 5~10분 걸릴 수 있어요.
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt
if not %errorlevel%==0 goto piperr
echo.
echo ==============================================
echo   설치가 끝났습니다.
echo   이 창을 닫고 웹앱실행.bat 을 실행하세요.
echo ==============================================
pause
exit /b 0
:piperr
echo.
echo [오류] 패키지 설치 중 문제가 생겼습니다. 위 메시지를 복사해서 알려 주세요.
pause
exit /b 1

:findpy
rem 진짜 파이썬 찾기 (윈도우의 'python' 가짜 바로가기는 건너뜀)
set "PY="
for %%V in (312 313 311 310) do if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
for %%V in (312 313 311 310) do if not defined PY if exist "%ProgramFiles%\Python%%V\python.exe" set "PY=%ProgramFiles%\Python%%V\python.exe"
if defined PY exit /b 0
python -c "import sys" >nul 2>nul
if %errorlevel%==0 set "PY=python"
if defined PY exit /b 0
py -3 -c "import sys" >nul 2>nul
if %errorlevel%==0 for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)"') do set "PY=%%I"
exit /b 0
