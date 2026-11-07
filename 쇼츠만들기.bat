@echo off
chcp 65001 > nul
set PYTHONUTF8=1
cd /d "%~dp0"
if "%~1"=="" (
  echo 쇼츠로 만들 영상 파일을 이 아이콘 위에 끌어다 놓으세요.
  pause
  exit /b
)
:loop
if "%~1"=="" goto done
python auto_shorts.py "%~1"
shift
goto loop
:done
pause
