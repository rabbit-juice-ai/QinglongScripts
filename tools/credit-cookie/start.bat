@echo off
chcp 65001 >nul
cd /d "%~dp0"
python cookie_tool.py
if errorlevel 1 pause
