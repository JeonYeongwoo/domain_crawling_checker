@echo off
setlocal
"%SystemRoot%\System32\chcp.com" 65001 >nul
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
python check.py %*
exit /b %errorlevel%
