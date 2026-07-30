@echo off
setlocal
cd /d "%~dp0"

if not exist "dist\index.html" (
  call npm.cmd run build
  if errorlevel 1 (
    pause
    exit /b 1
  )
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath 'node' -ArgumentList 'scripts/qa-server.mjs' -WorkingDirectory '%CD%' -WindowStyle Hidden"

timeout /t 1 /nobreak >nul
start "" "http://127.0.0.1:4179/#/sites"
