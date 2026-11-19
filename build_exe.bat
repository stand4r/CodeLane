@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ===================================================
echo CodeLane - Self-contained Windows Exe Build
echo ===================================================
echo.
echo Target executable will be windowless (no command prompt).
echo Python runtime and Qt dependencies will be bundled into dist\CodeLane\.
echo Target PC does NOT need Python installed.
echo.

python -c "import sys; print('Build Python:', sys.version); sys.exit(sys.version_info[:3] < (3, 15, 2) or sys.version_info[:2] != (3, 15))"
if errorlevel 1 (
    echo [ERROR] Activate a Python 3.15.2 or newer 3.15 environment.
    goto :fail
)

python -m pip install pyinstaller==6.22.3
if errorlevel 1 (
    echo [ERROR] Failed to verify or install PyInstaller.
    goto :fail
)

if not exist "src\PyScribe.ico" (
    echo [ERROR] src\PyScribe.ico not found.
    goto :fail
)

if not exist "CodeLane.spec" (
    echo [ERROR] CodeLane.spec not found.
    goto :fail
)

echo Cleaning previous build artifacts...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

echo.
echo Running PyInstaller with CodeLane.spec...
python -m PyInstaller --noconfirm --clean CodeLane.spec
if errorlevel 1 goto :fail

if not exist "dist\CodeLane\CodeLane.exe" (
    echo [ERROR] dist\CodeLane\CodeLane.exe was not created.
    goto :fail
)

echo.
echo ===================================================
echo BUILD SUCCESSFUL!
echo Output folder: %~dp0dist\CodeLane\
echo Executable:    %~dp0dist\CodeLane\CodeLane.exe
echo ===================================================
echo.
echo Next step: run installer\build_installer.bat to generate Inno Setup installer.
pause
exit /b 0

:fail
echo.
echo [ERROR] BUILD FAILED. Check console output above for details.
pause
exit /b 1
