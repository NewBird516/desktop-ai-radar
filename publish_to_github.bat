@echo off
chcp 65001 >nul
cd /d "%~dp0"

set REPO_URL=https://github.com/NewBird516/desktop-ai-radar.git

echo ==========================================================
echo   AI 雷达 - 推送到 GitHub
echo   仓库地址: %REPO_URL%
echo ==========================================================
echo.
echo   开始前请确认已做好这一件事：
echo   打开 https://github.com/new 创建仓库
echo     - Repository name: desktop-ai-radar
echo     - Public（公开）
echo     - 不要勾选 Add a README file / .gitignore / License
echo       （本地已经有了，勾了会冲突）
echo.
pause

where git >nul 2>nul
if errorlevel 1 (
    echo [X] 没找到 git，请先安装 https://git-scm.com
    pause
    exit /b 1
)

git rev-parse --git-dir >nul 2>nul
if errorlevel 1 (
    echo [X] 当前目录还不是 git 仓库。
    pause
    exit /b 1
)

git remote get-url origin >nul 2>nul
if errorlevel 1 (
    git remote add origin %REPO_URL%
) else (
    git remote set-url origin %REPO_URL%
)

git branch -M main
git push -u origin main

if errorlevel 1 (
    echo.
    echo [X] 推送失败，常见原因：
    echo   1. 网页上的空仓库还没创建
    echo   2. 还没登录 GitHub —— 在弹出的窗口里登录即可
    echo   3. 需要代理 —— 先执行：git config --global http.proxy http://127.0.0.1:端口
    echo   4. 也可以改用 GitHub Desktop 打开本文件夹推送
    pause
    exit /b 1
)

echo.
echo [OK] 推送成功！
echo      https://github.com/NewBird516/desktop-ai-radar
echo.
echo 建议顺手做的两件事：
echo   1. 仓库页 About 齿轮里加 topics: ai  news  rss  desktop-widget  python  pywebview  windows
echo   2. Releases - Create a new release - Tag: v1.0.0 发第一个版本
echo.
pause
