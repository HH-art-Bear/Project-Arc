@echo off
setlocal
set "APP_DIR=%~dp0"
rem Restart only an Arc server instance launched from this exact app folder.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$expected=[IO.Path]::GetFullPath($env:APP_DIR+'ollama-chat-server.py'); Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { $proc=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$_) -ErrorAction SilentlyContinue; if ($proc -and $proc.CommandLine -and $proc.CommandLine.Contains($expected)) { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } }"
timeout /t 1 /nobreak >nul
set "PYTHONW="
for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PYTHONW set "PYTHONW=%%P"
if defined PYTHONW (
    start "" "%PYTHONW%" "%APP_DIR%ollama-chat-server.py"
) else (
    echo Python was not found. Install Python 3, then double-click this launcher again.
    echo The app files are in: %APP_DIR%
    pause
)
endlocal
