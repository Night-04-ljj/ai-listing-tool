@echo off
rem ============================================================
rem  AI Listing tool - one click launcher
rem  This file is intentionally ASCII-only: cmd.exe parses .bat
rem  files with the OEM codepage, so non-ASCII text here breaks
rem  parsing. All Chinese messages live in start.py instead.
rem ============================================================
setlocal
cd /d "%~dp0"

py -V >nul 2>nul
if not errorlevel 1 goto :runpy

python -V >nul 2>nul
if not errorlevel 1 goto :runpython

echo.
echo  [ERROR] Python not found on this computer.
echo.
echo  Please install Python 3.10 or newer from:
echo      https://www.python.org/downloads/
echo.
echo  During installation, be sure to check:
echo      "Add python.exe to PATH"
echo.
pause
exit /b 1

:runpy
py "%~dp0start.py" %*
goto :end

:runpython
python "%~dp0start.py" %*

:end
pause
