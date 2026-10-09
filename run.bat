@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo =====================================================
    echo  First run detected - preparing environment...
    echo =====================================================
    where python >nul 2>nul
    if errorlevel 1 (
        echo [X] Python not found in PATH.
        echo     Please install Python 3.10+ from https://www.python.org
        echo     Remember to check "Add python.exe to PATH" during install.
        pause
        exit /b 1
    )
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -q pywebview requests feedparser beautifulsoup4 python-dateutil
    echo [OK] Environment ready.
)

start "" ".venv\Scripts\pythonw.exe" app.py
