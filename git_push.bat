@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo.
echo ============================================================
echo   Coach Certification -- Git Commit and Push
echo ============================================================
echo.

:: ?? Prompt for commit message ????????????????????????????????????????????????
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

echo  Committing: !COMMIT_MSG!
git commit -m "!COMMIT_MSG!"
if errorlevel 1 (
    echo  ERROR: git commit failed. Nothing to commit, or commit error.
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