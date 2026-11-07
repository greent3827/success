@echo off
chcp 65001 > nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist "쇼츠" mkdir "쇼츠"
python auto_shorts.py --watch "쇼츠"
pause
