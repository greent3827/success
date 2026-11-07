@echo off
chcp 65001 > nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist "업로드" mkdir "업로드"
python auto_editor.py --watch "업로드"
pause
