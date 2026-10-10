@echo off
setlocal
cd /d "%~dp0"

set "REPO_URL=https://github.com/NewBird516/desktop-ai-radar.git"

echo ==========================================================
echo   AI Radar - push to GitHub
echo   Repo: %REPO_URL%
echo ==========================================================
echo.
echo   Before we start, make sure you have created an EMPTY repo:
echo     1. Open https://github.com/new
echo     2. Repository name: desktop-ai-radar
echo     3. Visibility: Public
echo     4. Do NOT add README / .gitignore / license
echo        (they already exist locally, adding them will conflict)
echo.
pause

rem ---- locate git.exe ----
set "GITEXE="
git --version >nul 2>nul
if not errorlevel 1 set "GITEXE=git"
if not defined GITEXE if exist "%ProgramFiles%\Git\bin\git.exe" set "GITEXE=%ProgramFiles%\Git\bin\git.exe"
if not defined GITEXE if exist "%ProgramFiles(x86)%\Git\bin\git.exe" set "GITEXE=%ProgramFiles(x86)%\Git\bin\git.exe"
if not defined GITEXE if exist "%LocalAppData%\Programs\Git\bin\git.exe" set "GITEXE=%LocalAppData%\Programs\Git\bin\git.exe"

if not defined GITEXE (
    echo [X] git not found. Please install Git for Windows first:
    echo     https://git-scm.com/download/win
    pause
    exit /b 1
)

echo Using git: "%GITEXE%"
"%GITEXE%" --version

"%GITEXE%" rev-parse --git-dir >nul 2>nul
if errorlevel 1 (
    echo [X] This folder is not a git repository yet.
    pause
    exit /b 1
)

"%GITEXE%" remote get-url origin >nul 2>nul
if errorlevel 1 (
    "%GITEXE%" remote add origin %REPO_URL%
) else (
    "%GITEXE%" remote set-url origin %REPO_URL%
)

"%GITEXE%" branch -M main
"%GITEXE%" push -u origin main

if errorlevel 1 (
    echo.
    echo [X] Push failed. Common causes:
    echo   1. The empty repo on github.com is not created yet
    echo   2. Not logged in - sign in when the login popup appears
    echo   3. Network needs a proxy - run:
    echo      git config --global http.proxy http://127.0.0.1:PORT
    echo   4. Or use GitHub Desktop to push this folder instead
    pause
    exit /b 1
)

echo.
echo [OK] Pushed successfully!
echo      https://github.com/NewBird516/desktop-ai-radar
echo.
echo Recommended next steps:
echo   1. Repo page - About gear icon - add topics:
echo      ai, news, rss, desktop-widget, python, pywebview, windows
echo   2. Releases - Draft a new release - tag: v1.0.0
echo.
pause
