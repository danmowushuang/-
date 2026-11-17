@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw "%~dp0diskball.py"
) else (
  start "" pythonw "%~dp0diskball.py"
)
exit
