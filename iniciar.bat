@echo off
REM Arranca la aplicacion de trazabilidad en Windows (doble clic).
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  set PY=py
) else (
  set PY=python
)
%PY% -m pip install --quiet --disable-pip-version-check -r requirements.txt
start "" http://localhost:8000
%PY% app.py
pause
