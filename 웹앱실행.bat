@echo off
chcp 65001 > nul
set PYTHONUTF8=1
cd /d "%~dp0"
call :findpy
if defined PY goto run
echo [안내] 이 컴퓨터에 파이썬이 설치되어 있지 않습니다.
echo 같은 폴더의 처음설치.bat 을 먼저 실행해 주세요.
pause
exit /b 1
:run
echo 영상 자동화 스튜디오를 켭니다. 쓰는 동안 이 창을 닫지 마세요.
"%PY%" app.py
echo.
echo 웹앱이 꺼졌습니다. 위에 오류가 있으면 그 내용을 확인하세요.
pause
exit /b 0

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
