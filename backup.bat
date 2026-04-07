@echo off
setlocal enabledelayedexpansion

:: =============================================================================
::  Coach Certification -- Backup Utility
::
::  - Prompts for a short changelog note
::  - Appends the note to CHANGELOG.md (Keep a Changelog format)
::  - Stages all project files (excluding backup.bat) to a temp folder
::  - Adds a standalone backup_changelog_TIMESTAMP.txt to the staging folder
::  - Zips the staged folder as Coach_Certification_YYYY-MM-DD_HH-MM-SS.zip
::  - Saves the zip to the external backup destination
::  - Restoring: extract the zip to C:\Users\Admin\OneDrive\Documents\Coding\
::
::  Backup destination:
::    C:\Users\Admin\OneDrive\Documents\Coding\Coaching_Certifications_Backup\
:: =============================================================================

set "PROJECT_DIR=%~dp0"
if "!PROJECT_DIR:~-1!"=="\" set "PROJECT_DIR=!PROJECT_DIR:~0,-1!"

set "BACKUP_DIR=C:\Users\Admin\OneDrive\Documents\Coding\Coaching_Certifications_Backup"
set "STAGING_BASE=%TEMP%\Coach_Cert_Backup_Staging"
set "STAGING_DIR=%STAGING_BASE%\Coach_Certification"

echo.
echo ============================================================
echo   Coach Certification -- Backup Utility
echo ============================================================
echo.

:: ── Phase 1: Prompt for changelog note ──────────────────────────────────────
set /p "CHANGE_MSG=Enter changelog note: "
if "!CHANGE_MSG!"=="" set "CHANGE_MSG=No description provided."

:: ── Phase 2: Get current timestamp ──────────────────────────────────────────
for /f "delims=" %%T in ('powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'"') do set "TIMESTAMP=%%T"

set "ZIP_NAME=Coach_Certification_%TIMESTAMP%.zip"
set "ZIP_PATH=%BACKUP_DIR%\%ZIP_NAME%"
set "TEMP_PS=%TEMP%\coach_backup_%TIMESTAMP%.ps1"

echo.
echo  Timestamp : %TIMESTAMP%
echo  Target    : %ZIP_PATH%
echo.

:: ── Phase 3: Create backup destination if it does not exist ─────────────────
if not exist "%BACKUP_DIR%" (
    echo  Creating backup directory...
    mkdir "%BACKUP_DIR%"
    if errorlevel 1 (
        echo  ERROR: Could not create backup directory. Aborting.
        pause
        exit /b 1
    )
)

:: ── Phase 4: Append entry to CHANGELOG.md ───────────────────────────────────
::  Done BEFORE staging so the zip includes the updated CHANGELOG.md.
echo  Updating CHANGELOG.md...
(
echo $ts  = '%TIMESTAMP%'
echo $msg = $env:CHANGE_MSG
echo $f   = '%PROJECT_DIR%\CHANGELOG.md'
echo Add-Content -Path $f -Value '' -Encoding UTF8
echo Add-Content -Path $f -Value "## [Backup $ts]" -Encoding UTF8
echo Add-Content -Path $f -Value '### Backup Note' -Encoding UTF8
echo Add-Content -Path $f -Value "- $msg" -Encoding UTF8
) > "%TEMP_PS%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP_PS%"
if errorlevel 1 (
    echo  ERROR: Failed to update CHANGELOG.md. Aborting.
    del "%TEMP_PS%" 2>nul
    pause
    exit /b 1
)
del "%TEMP_PS%"

:: ── Phase 5: Stage project files (now includes updated CHANGELOG.md) ────────
echo  Staging project files...
if exist "%STAGING_BASE%" rmdir /S /Q "%STAGING_BASE%"
mkdir "%STAGING_DIR%"
if errorlevel 1 (
    echo  ERROR: Could not create staging directory. Aborting.
    pause
    exit /b 1
)

robocopy "%PROJECT_DIR%" "%STAGING_DIR%" /E /XF backup.bat /NFL /NJH /NJS > nul
:: Note: robocopy exit codes 0-7 are all success variants; do not check errorlevel here.

:: ── Phase 6: Write standalone backup_changelog .txt into the staging root ───
(
echo Backup timestamp: %TIMESTAMP%
echo Changelog note:   !CHANGE_MSG!
) > "%STAGING_DIR%\backup_changelog_%TIMESTAMP%.txt"

:: ── Phase 7: Create the zip archive ─────────────────────────────────────────
::  The zip root is Coach_Certification\ so extracting to:
::    C:\Users\Admin\OneDrive\Documents\Coding\
::  restores the full project folder.
echo  Creating zip archive...
(
echo Compress-Archive -Path '%STAGING_DIR%' -DestinationPath '%ZIP_PATH%' -Force
) > "%TEMP_PS%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP_PS%"
if errorlevel 1 (
    echo.
    echo  ERROR: Failed to create zip archive. Staging folder left for inspection:
    echo  %STAGING_DIR%
    del "%TEMP_PS%" 2>nul
    pause
    exit /b 1
)
del "%TEMP_PS%"

:: ── Phase 8: Clean up staging folder ────────────────────────────────────────
echo  Cleaning up staging folder...
rmdir /S /Q "%STAGING_BASE%"

echo.
echo ============================================================
echo   Backup complete!
echo.
echo   %ZIP_PATH%
echo ============================================================
echo.
pause
endlocal
