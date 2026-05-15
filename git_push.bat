@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo.
echo ============================================================
echo   Coach Certification -- Git Commit and Push
echo ============================================================
echo.

git rev-parse -q --verify REBASE_HEAD >nul 2>nul
if not errorlevel 1 (
    echo  ERROR: A git rebase is already in progress.
    echo  Resolve it before running this script again.
    echo.
    echo  Suggested commands:
    echo    1. git status
    echo    2. git rebase --continue
    echo    3. Or: git rebase --abort
    echo.
    pause
    exit /b 1
)

:: Prompt for commit message
set /p "COMMIT_MSG=Enter commit message: "
if "!COMMIT_MSG!"=="" set "COMMIT_MSG=Update project files"

echo.
echo  Staging all changes...
git add -A
if errorlevel 1 (
    echo  ERROR: git add failed. Aborting.
    pause
    exit /b 1
)

echo  Checking staged changes...
git diff --cached --quiet --exit-code
if not errorlevel 1 (
    echo.
    echo  No changes to commit. Everything is up to date.
    echo.
    pause
    exit /b 0
)

echo  Committing: !COMMIT_MSG!
git commit -m "!COMMIT_MSG!"
if errorlevel 1 (
    echo  ERROR: git commit failed.
    pause
    exit /b 1
)

echo  Pulling with rebase from origin/main...
git -c gc.auto=0 pull --rebase origin main
if errorlevel 1 (
    echo.
    echo  ERROR: pull --rebase failed.
    echo  Next steps:
    echo    1. git status
    echo    2. Resolve conflicted files
    echo    3. git add -A
    echo    4. git rebase --continue
    echo    5. Or: git rebase --abort
    echo.
    pause
    exit /b 1
)

echo  Pushing to origin/main...
git push origin main
if errorlevel 1 (
    echo  ERROR: git push failed. Check your credentials or network connection.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Push complete!
echo ============================================================
echo.
pause
endlocal