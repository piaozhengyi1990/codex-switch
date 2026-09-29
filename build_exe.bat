@echo off
title CodexSwitch Build Tool
setlocal

echo ========================================
echo   CodexSwitch Build Script
echo ========================================
echo.

cd /d "%~dp0"

echo [1/6] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found in PATH. Please install Python 3.11+ first.
    pause
    exit /b 1
)
python --version
echo.

echo [2/6] Checking virtual environment...
if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo Failed to create virtual environment!
        pause
        exit /b 1
    )
    echo Virtual environment created!
) else (
    echo Virtual environment exists, skip creation
)
echo.

echo [3/6] Installing dependencies...
if exist "requirements.lock" (
    echo Using pinned requirements.lock
    call venv\Scripts\pip.exe install -r requirements.lock --quiet
    if errorlevel 1 (
        echo Failed to install dependencies from lock file!
        pause
        exit /b 1
    )
) else (
    echo No requirements.lock, falling back to requirements.txt
    call venv\Scripts\pip.exe install -r requirements.txt --quiet
    if errorlevel 1 (
        echo Failed to install dependencies!
        pause
        exit /b 1
    )
    call venv\Scripts\pip.exe install "pyinstaller>=6.0,<7.0" --quiet
    if errorlevel 1 (
        echo Failed to install pyinstaller!
        pause
        exit /b 1
    )
)
echo Dependencies installed.
echo.

echo [4/6] Generating icon...
call venv\Scripts\python.exe generate_icon.py
if errorlevel 1 (
    echo Failed to generate icon!
    pause
    exit /b 1
)
echo.

echo [5/6] Cleaning old build...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
echo Clean completed.
echo.

echo [6/6] Building exe...
call venv\Scripts\python.exe -m PyInstaller --noconfirm --clean CodexSwitch.spec

if errorlevel 1 (
    echo.
    echo ========================================
    echo   Build FAILED! Check error messages
    echo ========================================
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Build SUCCESS!
echo ========================================
echo.
echo Output: dist\CodexSwitch.exe
if exist "dist\CodexSwitch.exe" (
    for %%A in ("dist\CodexSwitch.exe") do echo Size: %%~zA bytes
)
echo.

echo [+] Copying docs to dist...
if exist "README_CN.txt" (
    copy /y "README_CN.txt" "dist\README_CN.txt" >nul
    echo README_CN.txt copied to dist\
) else (
    echo Warning: README_CN.txt not found, skip copy
)
if exist "codex_cn.py" (
    copy /y "codex_cn.py" "dist\codex_cn.py" >nul
    echo codex_cn.py copied to dist\  ^(command line version^)
)

echo.
echo ========================================
echo   Press any key to exit...
echo ========================================
pause >nul
