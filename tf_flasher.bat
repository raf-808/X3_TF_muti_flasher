@echo off
setlocal
rem X3 batch flasher launcher: picks an interpreter that has tkinter,
rem then runs tf_flasher.py (it self-elevates to Administrator via UAC).
rem Interpreter list below is machine-specific; edit to match your box.
set "SCRIPT=%~dp0tf_flasher.py"
set "PY="
for %%P in (
    "C:\Users\admin\AppData\Roaming\uv\python\cpython-3.12.14-windows-x86_64-none\python.exe"
    "C:\Users\admin\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
    "python.exe"
) do (
    if not defined PY (
        %%~P -I -c "import tkinter" >nul 2>&1 && set "PY=%%~P"
    )
)
if not defined PY (
    echo [tf_flasher] No Python with tkinter found. GUI unavailable.
    echo CLI mode still works: python tf_flasher.py --list ^| --image ^| --flash
    pause
    exit /b 1
)
"%PY%" -I "%SCRIPT%"
if errorlevel 1 pause
endlocal
